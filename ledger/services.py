import calendar,csv,hashlib,io,json,logging,os,urllib.parse,urllib.request
from datetime import date,datetime
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from django.db import transaction,IntegrityError
from django.db.models import Sum
from django.utils import timezone
from .models import Account,JournalEntry,JournalLine,LockedPeriod,RecurringJV,Occurrence,BankRow,Setting,AuditEvent
log=logging.getLogger(__name__)
DEFAULTS={'company':'My Company','receivables':'1200','sales':'4100','output_vat':'2200','input_vat':'1300','payables':'2100','custom_definitions':'[]'}
ACCOUNTS=[('1100','Bank','asset'),('1200','Trade Receivables','asset'),('1300','Input VAT','asset'),('2100','Trade Payables','liability'),('2200','Output VAT','liability'),('3100','Owner Equity','equity'),('4100','Sales Revenue','income'),('5100','Cost of Goods Sold','cogs'),('6100','Operating Expenses','expense')]
def bootstrap():
    for code,name,typ in ACCOUNTS: Account.objects.get_or_create(code=code,defaults={'name':name,'type':typ})
    for k,v in DEFAULTS.items():Setting.objects.get_or_create(key=k,defaults={'value':v})
def setting(key):return Setting.objects.get(key=key).value
def audit(action,detail,user=None):AuditEvent.objects.create(action=action,detail=detail,user=user)
def cents(v):
    try:return int((Decimal(str(v).replace(',',''))*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    except (InvalidOperation,ValueError):raise ValueError('Invalid amount')
def parse_lines(raw):
    lines=[]
    for row in csv.reader(io.StringIO(raw)):
        if not row or not any(row):continue
        if len(row)!=3:raise ValueError('Each line needs account code, debit, credit')
        lines.append((row[0].strip(),cents(row[1] or 0),cents(row[2] or 0)))
    return lines
@transaction.atomic
def create_entry(day,reference,description,source,lines,user=None,source_key=None,custom=None):
    day=date.fromisoformat(str(day)[:10]); lines=list(lines)
    if len(lines)<2 or sum(l[1] for l in lines)<=0 or sum(l[1] for l in lines)!=sum(l[2] for l in lines):raise ValueError('Entry must have balanced positive debits and credits')
    validated=[]
    for code,dr,cr in lines:
        if dr<0 or cr<0 or bool(dr)==bool(cr):raise ValueError('Each line needs one positive side')
        validated.append((Account.objects.get(code=code,active=True),dr,cr))
    e=JournalEntry.objects.create(date=day,reference=reference or '',description=description or '',source=source,source_key=source_key,custom_fields=custom or {},created_by=user)
    JournalLine.objects.bulk_create([JournalLine(entry=e,account=a,debit=d,credit=cr) for a,d,cr in validated]);audit('draft_created',str(e),user)
    return e
@transaction.atomic
def post(entry_id,user=None):
    e=JournalEntry.objects.select_for_update().get(pk=entry_id)
    if e.status!='draft':raise ValueError('Only draft entries can be posted')
    if LockedPeriod.objects.filter(month=e.date.strftime('%Y-%m')).exists():raise ValueError('Accounting period is locked')
    totals=e.lines.aggregate(d=Sum('debit'),c=Sum('credit'))
    if e.lines.count()<2 or not totals['d'] or totals['d']!=totals['c']:raise ValueError('Entry does not balance')
    e.status='posted';e.posted_at=timezone.now();e.save(update_fields=['status','posted_at']);audit('posted',str(e),user);return e
@transaction.atomic
def reverse(entry_id,user=None):
    e=JournalEntry.objects.select_for_update().get(pk=entry_id)
    if e.status!='posted':raise ValueError('Only posted entries can be reversed')
    lines=[(l.account.code,l.credit,l.debit) for l in e.lines.select_related('account')]
    rev=create_entry(timezone.localdate(),'REV-'+str(e.pk),'Reversal of '+str(e),'reversal',lines,user)
    rev.reverses=e;rev.save(update_fields=['reverses']);post(rev.pk,user)
    e.status='reversed';e.save(update_fields=['status']);audit('reversed',f'{e.pk} by {rev.pk}',user);return rev
def shift_month(day,n):
    y=day.year+(day.month-1+n)//12;m=(day.month-1+n)%12+1
    return date(y,m,min(day.day,calendar.monthrange(y,m)[1]))
@transaction.atomic
def generate(user=None):
    count=0; today=timezone.localdate()
    for template in RecurringJV.objects.filter(active=True):
        interval={'monthly':1,'quarterly':3,'annual':12}[template.frequency]
        for n in range(1200):
            day=shift_month(template.start_date,n*interval)
            if day>today or (template.end_date and day>template.end_date):break
            if LockedPeriod.objects.filter(month=day.strftime('%Y-%m')).exists() or Occurrence.objects.filter(template=template,period_date=day).exists():continue
            e=create_entry(day,template.reference,template.description,'recurring',template.lines,user,f'recurring:{template.pk}:{day}')
            Occurrence.objects.create(template=template,period_date=day,entry=e);count+=1
    return count
def read_csv(upload):
    if not upload.name.lower().endswith('.csv') or upload.size>3000000:raise ValueError('Choose a CSV file under 3 MB')
    raw=upload.read();return list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))),hashlib.sha256(raw).hexdigest()

ACCOUNT_CSV_FIELDS=['code','name','type','parent_code','active']

def export_accounts():
    output=io.StringIO(newline='')
    writer=csv.writer(output)
    writer.writerow(ACCOUNT_CSV_FIELDS)
    for account in Account.objects.select_related('parent').order_by('code'):
        writer.writerow([account.code,account.name,account.type,
                         account.parent.code if account.parent else '',
                         'true' if account.active else 'false'])
    return '\ufeff'+output.getvalue()

@transaction.atomic
def import_accounts(upload,user=None):
    if not upload.name.lower().endswith('.csv') or upload.size>3000000:
        raise ValueError('Choose a CSV file under 3 MB')
    reader=csv.DictReader(io.StringIO(upload.read().decode('utf-8-sig'),newline=''),strict=True)
    if reader.fieldnames is None or sorted(reader.fieldnames)!=sorted(ACCOUNT_CSV_FIELDS):
        raise ValueError('CSV headers must be: '+', '.join(ACCOUNT_CSV_FIELDS))
    existing={a.code:a for a in Account.objects.select_related('parent')}
    parents={code:a.parent.code if a.parent else '' for code,a in existing.items()}
    pending={}
    for row in reader:
        row_number=reader.line_num
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f'Row {row_number}: incorrect number of columns')
        row={key:value.strip() for key,value in row.items()}
        code=row['code']
        if not code or code in pending:
            raise ValueError(f'Row {row_number}: missing or duplicate account code {code}')
        active=row['active'].lower()
        if active not in ('true','false','1','0','yes','no'):
            raise ValueError(f'Row {row_number}: active must be true or false')
        account=existing.get(code) or Account(code=code)
        account.name=row['name'];account.type=row['type'];account.active=active in ('true','1','yes')
        account.full_clean()
        pending[code]=account
        parents[code]=row['parent_code']
    if not pending:raise ValueError('CSV contains no accounts')
    for code in pending:
        seen=set();current=code
        while current:
            if current not in parents:raise ValueError(f'Account {code}: unknown parent {current}')
            if current in seen:raise ValueError(f'Account {code}: parent hierarchy contains a cycle')
            seen.add(current);current=parents[current]
    created=sum(code not in existing for code in pending)
    # Save all accounts first so parents can occur anywhere in the CSV.
    for account in pending.values():account.save()
    all_accounts={**existing,**pending}
    for code,account in pending.items():
        account.parent=all_accounts.get(parents[code])
        account.full_clean();account.save(update_fields=['parent'])
    updated=len(pending)-created
    audit('accounts_import',f'{created} created, {updated} updated',user)
    return created,updated
@transaction.atomic
def import_expenses(upload,user=None):
    rows,digest=read_csv(upload);count=skipped=0
    for i,r in enumerate(rows,1):
        key=f'expense:{digest}:{i}'
        if JournalEntry.objects.filter(source_key=key).exists():skipped+=1;continue
        net=cents(r['net_amount']);vat=cents(r.get('vat_amount') or '0')
        if net<=0 or vat<0:raise ValueError(f'Row {i}: invalid amounts')
        lines=[(r['account_code'],net,0)]
        if vat:lines.append((setting('input_vat'),vat,0))
        lines.append((r.get('credit_account') or setting('payables'),0,net+vat))
        supplier=(r.get('supplier') or '').strip()
        supplier_trn=(r.get('supplier_trn') or '').strip()
        trn_alias=(r.get('trn') or '').strip()
        if supplier_trn and trn_alias and supplier_trn!=trn_alias:
            raise ValueError(f'Row {i}: supplier_trn and trn disagree')
        custom={k:v for k,v in r.items() if k and k.startswith('custom_')}
        custom.update(supplier=supplier,supplier_trn=supplier_trn or trn_alias)
        create_entry(r['date'],r.get('reference'),(supplier+' '+(r.get('description') or '')).strip(),'csv_expense',lines,user,key,custom);count+=1
    audit('expense_import',f'{count} drafts, {skipped} duplicates',user);return count,skipped
def preview_bank_statement(upload):
    rows,digest=read_csv(upload)
    if not rows:
        raise ValueError('The statement contains no transaction rows')
    aliases={'value date':'date','reference':'ref','ref/cheque no':'ref',
             'debit amount':'debit','credit amount':'credit'}
    result=[]
    for i,raw in enumerate(rows,1):
        try:
            if None in raw:
                raise ValueError('Too many columns; quote amounts containing commas')
            row={aliases.get(k.strip().lower(),k.strip().lower()):(v or '').strip() for k,v in raw.items()}
            if not {'date','description','ref','debit','credit'} <= row.keys():
                raise ValueError('Required columns: date, Description, ref, debit, credit')
            if not any(row.values()):
                continue
            day=None
            for fmt in ('%Y-%m-%d','%d/%m/%Y','%d-%m-%Y'):
                try:
                    day=datetime.strptime(row['date'],fmt).date()
                    break
                except ValueError:
                    pass
            if day is None:
                raise ValueError('Use dates in YYYY-MM-DD or DD/MM/YYYY format')
            amounts=[]
            for field in ('debit','credit'):
                value=Decimal(row[field].replace(',','') or '0')
                if not value.is_finite() or value < 0 or value > Decimal('999999999999.99') or value*100 != (value*100).to_integral_value():
                    raise ValueError('Amounts must be nonnegative with at most two decimal places')
                amounts.append(int(value*100))
            debit,credit=amounts
            if bool(debit)==bool(credit):
                raise ValueError('Enter a positive debit or credit, with the other side blank or zero')
            if len(row['ref'])>100:
                raise ValueError('Reference must be at most 100 characters')
            result.append({'date':day.isoformat(),'reference':row['ref'],'description':row['description'],
                           'debit':debit,'credit':credit,'line':i})
        except (ValueError,InvalidOperation) as exc:
            raise ValueError(f'Row {i}: {exc}') from exc
    if not result:
        raise ValueError('The statement contains no transaction rows')
    return {'digest':digest,'rows':result}


@transaction.atomic
def save_bank_statement(statement,bank_account,counter_accounts,user=None):
    if len(statement['rows']) != len(counter_accounts):
        raise ValueError('Select an opposite account for every row')
    count=0
    for row,counter in zip(statement['rows'],counter_accounts):
        if not bank_account.active or not counter.active or bank_account.pk==counter.pk:
            raise ValueError('Select active, different bank and opposite accounts')
        _,created=BankRow.objects.get_or_create(
            source_key=f"bank:{statement['digest']}:{bank_account.pk}:{row['line']}",
            defaults={'date':row['date'],'reference':row['reference'],'description':row['description'],
                      'amount':row['credit']-row['debit'],'bank_account':bank_account,'counter_account':counter})
        count+=created
    audit('bank_import',f'{count} rows',user)
    return count,len(statement['rows'])-count


@transaction.atomic
def import_bank(upload,user=None):
    rows,digest=read_csv(upload);count=0
    for i,r in enumerate(rows,1):
        amount=cents(r['amount'])
        if not amount:raise ValueError(f'Row {i}: zero amount')
        a=Account.objects.get(code=r['bank_account'],active=True);b=Account.objects.get(code=r['counter_account'],active=True)
        _,created=BankRow.objects.get_or_create(source_key=f'bank:{digest}:{i}',defaults={'date':date.fromisoformat(r['date']),'reference':r.get('reference',''),'description':r.get('description',''),'amount':amount,'bank_account':a,'counter_account':b})
        count+=created
    audit('bank_import',f'{count} rows',user);return count
@transaction.atomic
def sync_dolibarr(since=None,user=None):
    base=os.getenv('DOLIBARR_URL','').rstrip('/');key=os.getenv('DOLIBARR_API_KEY','')
    if not base.startswith(('http://','https://')) or not key:raise ValueError('DOLIBARR_URL and DOLIBARR_API_KEY are required')
    imported=skipped=0
    page=0
    while True:
        # Include both unpaid (1) and paid (2) validated customer invoices.
        url=base+'/invoices?'+urllib.parse.urlencode({'sortfield':'t.rowid','sortorder':'ASC','limit':100,'page':page,'sqlfilters':'((t.fk_statut:=:1) or (t.fk_statut:=:2))'})
        req=urllib.request.Request(url,headers={'DOLAPIKEY':key,'Accept':'application/json'})
        with urllib.request.urlopen(req,timeout=30) as response:payload=json.load(response)
        if not isinstance(payload,list):raise ValueError('Unexpected Dolibarr response')
        for inv in payload:
            docid=str(inv.get('id') or inv.get('rowid') or '')
            if not docid:raise ValueError('Invoice missing ID')
            sk=f'dolibarr:invoice:{docid}'
            net=cents(inv.get('total_ht') or 0);vat=cents(inv.get('total_tva') or 0)
            invoice_type=str(inv.get('type','')).strip()
            # Dolibarr type 2 is a credit note; type 3 is a deposit invoice.
            credit=invoice_type=='2' or net<0
            document_kind='credit note' if credit else 'invoice'
            description=f'Dolibarr {document_kind} {docid}'
            if credit:
                # Refresh only the old generated label, including posted entries.
                # Existing journal lines and user-written descriptions are preserved.
                JournalEntry.objects.filter(source_key=sk,source='dolibarr',description=f'Dolibarr invoice {docid}').update(description=description)
            if JournalEntry.objects.filter(source_key=sk).exists():skipped+=1;continue
            stamp=inv.get('date') or inv.get('datef')
            if not stamp:raise ValueError(f'Invoice {docid} missing date')
            day=datetime.fromtimestamp(int(stamp)).date() if str(stamp).isdigit() else date.fromisoformat(str(stamp)[:10])
            if since and day<since:continue
            if not net and not vat:skipped+=1;continue
            net=abs(net);vat=abs(vat)
            lines=[(setting('sales'),net,0),(setting('output_vat'),vat,0),(setting('receivables'),0,net+vat)] if credit else [(setting('receivables'),net+vat,0),(setting('sales'),0,net),(setting('output_vat'),0,vat)]
            create_entry(day,inv.get('ref'),description,'dolibarr',[l for l in lines if l[1] or l[2]],user,sk,{'dolibarr_id':docid,'dolibarr_type':invoice_type});imported+=1
        if len(payload)<100:break
        page+=1
    audit('dolibarr_sync',f'{imported} drafts, {skipped} skipped',user);return imported,skipped
