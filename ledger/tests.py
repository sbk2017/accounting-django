from datetime import date
import io,json
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
from django.contrib.auth import get_user_model
from django.test import TestCase,override_settings
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from . import services as svc
from .models import Account,JournalEntry,LockedPeriod,RecurringJV,Occurrence

@override_settings(ALLOWED_HOSTS=['testserver'])
class ExpenseTRNTests(TestCase):
    def setUp(self):
        svc.bootstrap()

    def upload(self,extra_header='',extra_value=''):
        text=('date,reference,supplier,description,account_code,net_amount,vat_amount,credit_account,custom_department'+extra_header+'\n'
              '2026-09-01,EXP-001,Example Supplier,Office supplies,6100,100.00,5.00,2100,Operations'+extra_value+'\n')
        return SimpleUploadedFile('expenses.csv',text.encode())

    def test_trn_is_retained_as_text_and_visible_without_changing_amounts(self):
        self.assertEqual(svc.import_expenses(self.upload(',supplier_trn',', 001234567890123 ')),(1,0))
        entry=JournalEntry.objects.get()
        self.assertEqual(entry.custom_fields,{'supplier':'Example Supplier','supplier_trn':'001234567890123','custom_department':'Operations'})
        self.assertEqual(entry.lines.get(account__code='1300').debit,500)
        self.assertEqual(entry.lines.get(account__code='2100').credit,10500)
        self.assertEqual(svc.import_expenses(self.upload(',supplier_trn',', 001234567890123 ')),(0,1))
        user=get_user_model().objects.create_user('trn-reviewer')
        self.client.force_login(user)
        response=self.client.get(reverse('journal_detail',args=[entry.pk]))
        self.assertContains(response,'001234567890123')
        self.assertContains(response,'Example Supplier')

    def test_old_files_blank_values_and_alias(self):
        for header,value,expected in [('', '', ''),(',supplier_trn',',',''),(',trn',',001234567890123','001234567890123')]:
            with self.subTest(header=header):
                self.assertEqual(svc.import_expenses(self.upload(header,value)),(1,0))
                self.assertEqual(JournalEntry.objects.latest('pk').custom_fields['supplier_trn'],expected)

    def test_conflicting_trn_columns_roll_back_file(self):
        upload=self.upload(',supplier_trn,trn',',001234567890123,001234567890123')
        content=upload.read()+b'2026-09-02,EXP-002,Other supplier,Office,6100,100,5,2100,Operations,111,222\n'
        with self.assertRaisesMessage(ValueError,'Row 2: supplier_trn and trn disagree'):
            svc.import_expenses(SimpleUploadedFile('expenses.csv',content))
        self.assertFalse(JournalEntry.objects.exists())

@override_settings(ALLOWED_HOSTS=['testserver'])
class AccountCSVTests(TestCase):
    def setUp(self):
        svc.bootstrap()
        self.user=get_user_model().objects.create_user('accounts',password='test-password',is_staff=True)
        self.client.force_login(self.user)

    def upload(self,text):
        return SimpleUploadedFile('accounts.csv',text.encode('utf-8'),content_type='text/csv')

    def test_download_restore_round_trip_and_repeat(self):
        parent=Account.objects.create(code='0099',name='Parent',type='asset')
        Account.objects.create(code='0001',name='Cash, "reserve" نقد',type='asset',parent=parent,active=False)
        expected=list(Account.objects.values_list('code','name','type','parent__code','active'))
        response=self.client.get(reverse('accounts'),{'download':'csv'})
        self.assertEqual(response.status_code,200)
        self.assertIn('chart-of-accounts.csv',response['Content-Disposition'])
        Account.objects.filter(code='0001').delete()
        Account.objects.filter(code='0099').delete()
        Account.objects.filter(code='4100').update(name='Changed')
        csv_text=response.content.decode('utf-8')
        result=self.client.post(reverse('accounts'),{'action':'import_csv','file':self.upload(csv_text)},HTTP_REFERER=reverse('accounts'))
        self.assertEqual(result.status_code,302)
        self.assertEqual(list(Account.objects.values_list('code','name','type','parent__code','active')),expected)
        self.assertEqual(svc.import_accounts(self.upload(csv_text)),(0,len(expected)))

    def test_validation_is_atomic(self):
        header='code,name,type,parent_code,active\n'
        bad_rows=[
            'X,Example,asset,MISSING,true\n',
            'X,Example,asset,Y,true\nY,Parent,asset,X,true\n',
            'X,Example,asset,X,true\n',
            'X,Example,invalid,,true\n',
            'X,Example,asset,,maybe\n',
            'X,Example,asset,,true\nX,Duplicate,asset,,true\n',
            'X,Example,asset\n',
        ]
        for rows in bad_rows:
            with self.subTest(rows=rows):
                with self.assertRaises((ValueError,ValidationError)):
                    svc.import_accounts(self.upload(header+'4100,Renamed,income,,true\n'+rows))
                self.assertEqual(Account.objects.get(code='4100').name,'Sales Revenue')
                self.assertFalse(Account.objects.filter(code='X').exists())
        with self.assertRaisesMessage(ValueError,'headers'):
            svc.import_accounts(self.upload('code,name\nX,Example\n'))
        with self.assertRaisesMessage(ValueError,'no accounts'):
            svc.import_accounts(self.upload(header))

    def test_used_account_type_protection_and_absent_accounts_preserved(self):
        svc.create_entry('2026-09-01','','','manual',[('1200',100,0),('4100',0,100)])
        with self.assertRaises(ValidationError):
            svc.import_accounts(self.upload('code,name,type,parent_code,active\n4100,Sales,asset,,true\n'))
        count=Account.objects.count()
        self.assertEqual(svc.import_accounts(self.upload('code,name,type,parent_code,active\n4100,New sales,income,,false\n')),(0,1))
        self.assertEqual(Account.objects.count(),count)
        self.assertFalse(Account.objects.get(code='4100').active)

    def test_upload_requires_staff_and_download_requires_login(self):
        self.user.is_staff=False;self.user.save()
        response=self.client.post(reverse('accounts'),{'action':'import_csv','file':self.upload('code,name,type,parent_code,active\nX,Example,asset,,true\n')})
        self.assertEqual(response.status_code,400)
        self.assertFalse(Account.objects.filter(code='X').exists())
        self.client.logout()
        self.assertEqual(self.client.get(reverse('accounts'),{'download':'csv'}).status_code,302)

@patch.dict('os.environ',{'DOLIBARR_URL':'https://dolibarr.example/api/index.php','DOLIBARR_API_KEY':'test-key'})
class DolibarrSyncTests(TestCase):
    def setUp(self):
        svc.bootstrap()

    def invoice(self,docid,**changes):
        return {'id':docid,'ref':f'INV-{docid}','date':'2026-09-10',
                'total_ht':'100','total_tva':'5','type':'0',**changes}

    def response(self,payload):
        return io.StringIO(json.dumps(payload))

    @patch('ledger.services.urllib.request.urlopen')
    def test_credit_notes_reduce_revenue_vat_and_receivables(self,fetch):
        invoices=[self.invoice(1,type='2'),self.invoice(2,type=2,total_ht='-100',total_tva='-5')]
        fetch.side_effect=lambda *args,**kwargs:self.response(invoices)
        self.assertEqual(svc.sync_dolibarr(),(2,0))
        for entry in JournalEntry.objects.all():
            self.assertIn('credit note',entry.description)
            self.assertEqual(entry.lines.get(account__code='4100').debit,10000)
            self.assertEqual(entry.lines.get(account__code='2200').debit,500)
            self.assertEqual(entry.lines.get(account__code='1200').credit,10500)
            svc.post(entry.pk)
        from .views import summary
        balances={row['code']:row['normal'] for row in summary('2026-09')}
        self.assertEqual(balances['4100'],-20000)
        self.assertEqual(balances['1200'],-21000)

    @patch('ledger.services.urllib.request.urlopen')
    def test_deposit_is_not_a_credit_note(self,fetch):
        fetch.return_value=self.response([self.invoice(1,type='3')])
        svc.sync_dolibarr()
        entry=JournalEntry.objects.get()
        self.assertEqual(entry.lines.get(account__code='4100').credit,10000)
        self.assertEqual(entry.lines.get(account__code='1200').debit,10500)

    @patch('ledger.services.urllib.request.urlopen')
    def test_resync_relabels_existing_credit_note_without_changing_posting(self,fetch):
        fetch.side_effect=lambda *args,**kwargs:self.response([self.invoice(1,type='2')])
        svc.sync_dolibarr()
        entry=JournalEntry.objects.get()
        entry.description='Dolibarr invoice 1';entry.save()
        svc.post(entry.pk)
        before=list(entry.lines.values())
        self.assertEqual(svc.sync_dolibarr(),(0,1))
        entry.refresh_from_db()
        self.assertEqual(entry.description,'Dolibarr credit note 1')
        self.assertEqual(entry.status,'posted')
        self.assertEqual(list(entry.lines.values()),before)
        entry.description='My custom description';entry.save()
        svc.sync_dolibarr()
        entry.refresh_from_db()
        self.assertEqual(entry.description,'My custom description')

    @patch('ledger.services.urllib.request.urlopen')
    def test_sync_includes_paid_and_unpaid_and_is_idempotent(self,fetch):
        invoices=[self.invoice(1,status='1'),self.invoice(2,status='2')]
        fetch.side_effect=lambda *args,**kwargs:self.response(invoices)
        self.assertEqual(svc.sync_dolibarr(),(2,0))
        query=parse_qs(urlsplit(fetch.call_args.args[0].full_url).query)
        self.assertEqual(query['sqlfilters'],['((t.fk_statut:=:1) or (t.fk_statut:=:2))'])
        self.assertEqual(svc.sync_dolibarr(),(0,2))
        for entry in JournalEntry.objects.all():
            self.assertEqual(entry.status,'draft')
            self.assertEqual(entry.lines.get(account__code='1200').debit,10500)

    @patch('ledger.services.urllib.request.urlopen')
    def test_sync_fetches_next_page_after_full_page(self,fetch):
        fetch.side_effect=[self.response([self.invoice(i) for i in range(1,101)]),
                           self.response([self.invoice(101)])]
        self.assertEqual(svc.sync_dolibarr(),(101,0))
        self.assertEqual([parse_qs(urlsplit(c.args[0].full_url).query)['page']
                          for c in fetch.call_args_list],[['0'],['1']])

    @patch('ledger.services.urllib.request.urlopen')
    def test_sync_continues_past_one_hundred_pages(self,fetch):
        # Zero-value invoices keep this pagination regression inexpensive.
        full_page=[self.invoice(i,total_ht='0',total_tva='0') for i in range(1,101)]
        fetch.side_effect=lambda req,**kwargs:self.response(
            full_page if int(parse_qs(urlsplit(req.full_url).query)['page'][0])<101 else [])
        with patch.object(svc.JournalEntry.objects,'filter') as entries:
            entries.return_value.exists.return_value=False
            self.assertEqual(svc.sync_dolibarr(),(0,10100))
        self.assertEqual(fetch.call_count,102)

    @patch('ledger.services.urllib.request.urlopen')
    def test_date_filter_and_api_failure_rollback(self,fetch):
        fetch.side_effect=[self.response([self.invoice(1,date='2026-08-31'),self.invoice(2)])]
        self.assertEqual(svc.sync_dolibarr(since=date(2026,9,1)),(1,0))
        self.assertFalse(JournalEntry.objects.filter(reference='INV-1').exists())
        fetch.side_effect=[self.response([self.invoice(i) for i in range(3,103)]),
                           OSError('API unavailable')]
        with self.assertRaisesMessage(OSError,'API unavailable'):
            svc.sync_dolibarr()
        self.assertEqual(JournalEntry.objects.count(),1)

@override_settings(ALLOWED_HOSTS=['testserver'])
class DashboardPeriodTests(TestCase):
    def setUp(self):
        svc.bootstrap()
        self.user=get_user_model().objects.create_user('dashboard',password='test-password')
        self.client.force_login(self.user)

    def sale(self,day,amount,credit=False,posted=True):
        lines=[('1200',amount,0),('4100',0,amount)]
        if credit:lines=[(code,cr,dr) for code,dr,cr in lines]
        entry=svc.create_entry(day,'','', 'manual',lines)
        if posted:svc.post(entry.pk)
        return entry

    @patch('ledger.views.timezone.localdate',return_value=date(2026,9,15))
    def test_period_totals_credit_notes_costs_and_cutoff(self,_today):
        self.sale('2026-01-01',10000)
        self.sale('2026-08-31',20000)
        self.sale('2026-09-01',30000)
        self.sale('2026-09-15',5000,credit=True)
        self.sale('2026-09-16',90000)
        self.sale('2026-09-01',90000,posted=False)
        self.sale('2025-12-31',90000)
        costs=svc.create_entry('2026-09-10','','','manual',
            [('5100',4000,0),('6100',1000,0),('2100',0,5000)])
        svc.post(costs.pk)
        response=self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code,200)
        current,last,ytd=response.context['periods']
        self.assertEqual([p['sales'] for p in (current,last,ytd)],['250.00','200.00','550.00'])
        self.assertEqual((current['gross'],current['expenses'],current['profit']),('210.00','10.00','200.00'))
        self.assertEqual(ytd['profit'],'500.00')
        self.assertEqual(response.context['drafts'],1)
        for label in ('Current month','Last month','Year to date'):
            self.assertContains(response,label)

    @patch('ledger.views.timezone.localdate',return_value=date(2026,1,1))
    def test_january_uses_previous_december_and_includes_reversals(self,_today):
        december=self.sale('2025-12-31',10000)
        svc.reverse(december.pk)
        self.sale('2026-01-01',20000)
        response=self.client.get(reverse('dashboard'))
        current,last,ytd=response.context['periods']
        self.assertEqual(last['start'],date(2025,12,1))
        self.assertEqual(last['end'],date(2025,12,31))
        self.assertEqual([p['sales'] for p in (current,last,ytd)],['100.00','100.00','100.00'])

@override_settings(ALLOWED_HOSTS=['testserver'])
class AccountingFlowTests(TestCase):
    def setUp(self):
        svc.bootstrap()
        self.user=get_user_model().objects.create_superuser('sultan','s@example.test','a-long-test-password')
    def test_login_post_reports_and_locked_month(self):
        response=self.client.post(reverse('login'),{'username':'sultan','password':'a-long-test-password'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code,200)
        e=svc.create_entry('2026-09-10','S1','sale','manual',[('1200',10500,0),('4100',0,10000),('2200',0,500)],self.user)
        response=self.client.post(reverse('post_journal',args=[e.pk]))
        self.assertEqual(response.status_code,302)
        e.refresh_from_db();self.assertEqual(e.status,'posted')
        report=self.client.get(reverse('reports')+'?year=2026&month=9')
        self.assertEqual(report.status_code,200)
        self.assertContains(report,'100.00')
        LockedPeriod.objects.create(month='2026-09')
        late=svc.create_entry('2026-09-11','','late','manual',[('6100',100,0),('2100',0,100)],self.user)
        with self.assertRaisesMessage(ValueError,'locked'):svc.post(late.pk,self.user)
    def test_recurring_is_idempotent(self):
        r=RecurringJV.objects.create(name='Rent',start_date=date.today().replace(day=1),frequency='monthly',lines=[['6100',10000,0],['2100',0,10000]])
        self.assertEqual(svc.generate(self.user),1)
        self.assertEqual(svc.generate(self.user),0)
        self.assertEqual(Occurrence.objects.filter(template=r).count(),1)
    def test_wrong_password_stays_on_login(self):
        response=self.client.post(reverse('login'),{'username':'sultan','password':'wrong'})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Incorrect username or password')

@override_settings(ALLOWED_HOSTS=['testserver'])
class JournalScreenTests(TestCase):
    def setUp(self):
        svc.bootstrap()
        self.user=get_user_model().objects.create_superuser('reviewer','r@example.test','a-long-test-password')
        self.client.force_login(self.user)
    def entry(self,reference='CSV-01',day='2026-09-10',amount=12345):
        return svc.create_entry(day,reference,'Office expenses','csv_expense',[('6100',amount,0),('2100',0,amount)],self.user)
    def test_pagination_amount_and_column_filters(self):
        for i in range(23):self.entry(reference=f'BATCH-{i}')
        response=self.client.get(reverse('journals'))
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.context['entries']),20)
        self.assertContains(response,'123.45')
        self.assertEqual(len(self.client.get(reverse('journals')+'?page=2').context['entries']),3)
        target=self.entry(reference='SPECIAL',day='2026-08-01',amount=99900)
        query={'id':target.pk,'date_from':'2026-08-01','date_to':'2026-08-31','reference':'special','description':'Office','source':'csv','status':'draft','amount_min':'999','amount_max':'999'}
        result=self.client.get(reverse('journals'),query)
        self.assertEqual([e.pk for e in result.context['entries']],[target.pk])
        self.assertEqual(self.client.get(reverse('journals'),{'amount_min':'bad'}).status_code,200)
        self.assertEqual(len(self.client.get(reverse('journals'),{'amount_min':'bad'}).context['entries']),0)
    def test_bulk_posting_and_locked_batch_rollback(self):
        one=self.entry();two=self.entry()
        response=self.client.post(reverse('post_selected_journals'),{'selected':[one.pk,two.pk],'return_query':'status=draft&page=1'})
        self.assertEqual(response.status_code,302)
        self.assertIn('status=draft',response.url)
        self.assertEqual(JournalEntry.objects.filter(pk__in=[one.pk,two.pk],status='posted').count(),2)
        valid=self.entry(day='2026-08-01');locked=self.entry(day='2026-09-01')
        LockedPeriod.objects.create(month='2026-09')
        self.client.post(reverse('post_selected_journals'),{'selected':[valid.pk,locked.pk]})
        valid.refresh_from_db();locked.refresh_from_db()
        self.assertEqual(valid.status,'draft');self.assertEqual(locked.status,'draft')
        self.assertFalse(valid.posted_at)
    def test_bulk_permissions_and_stale_selection(self):
        e=self.entry();svc.post(e.pk,self.user)
        other=self.entry()
        self.client.post(reverse('post_selected_journals'),{'selected':[other.pk,e.pk]})
        other.refresh_from_db();self.assertEqual(other.status,'draft')
        self.user.is_staff=False;self.user.save()
        self.assertEqual(self.client.post(reverse('post_selected_journals'),{'selected':[other.pk]}).status_code,400)
        other.refresh_from_db();self.assertEqual(other.status,'draft')
    def form_data(self):
        from .models import Account
        return {'date':'2026-09-20','reference':'NEW','description':'Selected accounts',
                'lines-TOTAL_FORMS':'3','lines-INITIAL_FORMS':'0','lines-MIN_NUM_FORMS':'0','lines-MAX_NUM_FORMS':'200',
                'lines-0-account':Account.objects.get(code='6100').pk,'lines-0-debit':'125.50','lines-0-credit':'',
                'lines-1-account':Account.objects.get(code='2100').pk,'lines-1-debit':'','lines-1-credit':'125.50',
                'lines-2-account':'','lines-2-debit':'','lines-2-credit':'','lines-2-DELETE':'on'}
    def test_field_form_creates_balanced_entry_and_keeps_errors(self):
        response=self.client.get(reverse('journals'))
        self.assertContains(response,'Operating Expenses')
        self.assertContains(response,'name="lines-0-account"')
        response=self.client.post(reverse('journals'),self.form_data())
        self.assertEqual(response.status_code,302)
        entry=JournalEntry.objects.get(reference='NEW')
        self.assertEqual(entry.lines.count(),2)
        self.assertEqual(entry.lines.get(account__code='6100').debit,12550)
        invalid=self.form_data();invalid['reference']='INVALID';invalid['lines-1-credit']='120'
        response=self.client.post(reverse('journals'),invalid)
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Total debits and credits must be equal')
        self.assertContains(response,'value="INVALID"')
        self.assertFalse(JournalEntry.objects.filter(reference='INVALID').exists())


@override_settings(ALLOWED_HOSTS=['testserver'])
class BankStatementTests(TestCase):
    def setUp(self):
        svc.bootstrap()
        self.client.force_login(get_user_model().objects.create_user('bank-reviewer'))
        self.bank=Account.objects.get(code='1100')
        self.expense=Account.objects.get(code='6100')
        self.receivable=Account.objects.get(code='1200')

    def upload(self):
        return SimpleUploadedFile('statement.csv',b'date,Description,ref,debit,credit\n30/09/2026,Charges,FEE,5,0\n2026-09-30,Receipt,PAY,,105\n')

    def preview(self):
        response=self.client.post(reverse('imports'),{'kind':'bank','file':self.upload()})
        self.assertRedirects(response,reverse('bank_statement'))
        return self.client.session['bank_statement']['token']

    def selection(self,token):
        return {'token':token,'bank_account':self.bank.pk,
                'line-0-counter_account':self.expense.pk,'line-1-counter_account':self.receivable.pk}

    def test_review_save_duplicates_and_journal_directions(self):
        from .models import BankRow
        token=self.preview()
        self.assertFalse(BankRow.objects.exists())
        response=self.client.get(reverse('bank_statement'))
        self.assertContains(response,'Charges')
        self.assertContains(response,'Receipt')
        self.assertContains(response,'105.00')
        response=self.client.post(reverse('bank_statement'),self.selection(token))
        self.assertRedirects(response,reverse('bank'))
        self.assertEqual(BankRow.objects.count(),2)
        for ref,amount,debit,credit in [('FEE',-500,0,500),('PAY',10500,10500,0)]:
            row=BankRow.objects.get(reference=ref)
            self.assertEqual(row.amount,amount)
            self.client.post(reverse('bank_draft',args=[row.pk]))
            row.refresh_from_db()
            line=row.draft_entry.lines.get(account=self.bank)
            self.assertEqual((line.debit,line.credit),(debit,credit))
        token=self.preview()
        self.client.post(reverse('bank_statement'),self.selection(token))
        self.assertEqual(BankRow.objects.count(),2)

    def test_invalid_selection_preserves_review_without_partial_save(self):
        from .models import BankRow
        token=self.preview()
        data=self.selection(token)
        data['line-1-counter_account']=self.bank.pk
        response=self.client.post(reverse('bank_statement'),data)
        self.assertContains(response,'Choose an account different')
        self.assertContains(response,'Charges')
        self.assertFalse(BankRow.objects.exists())
        self.expense.active=False
        self.expense.save()
        response=self.client.post(reverse('bank_statement'),self.selection(token))
        self.assertContains(response,'Select a valid choice')
        self.assertFalse(BankRow.objects.exists())

    def test_replaced_upload_cannot_be_saved_from_stale_tab(self):
        from .models import BankRow
        old=self.preview()
        self.preview()
        response=self.client.post(reverse('bank_statement'),self.selection(old))
        self.assertContains(response,'replaced by another upload')
        self.assertFalse(BankRow.objects.exists())

    def test_parser_rejects_invalid_rows_and_accepts_export_headers(self):
        for amounts in ['1,1','0,0','-1,0','NaN,0','1.001,0']:
            with self.subTest(amounts=amounts),self.assertRaises(ValueError):
                svc.preview_bank_statement(SimpleUploadedFile('bank.csv',
                    ('date,Description,ref,debit,credit\n2026-09-30,Test,R,'+amounts+'\n').encode()))
        result=svc.preview_bank_statement(SimpleUploadedFile('bank.csv',
            b' Value Date ,Description,Ref/Cheque No,Debit Amount,Credit Amount,Balance\n30/09/2026,Test,001,"1,234.50",0,10\n'))
        self.assertEqual(result['rows'][0]['debit'],123450)
        self.assertEqual(result['rows'][0]['reference'],'001')


@override_settings(ALLOWED_HOSTS=['testserver'])
class SessionCSRFTests(TestCase):
    def setUp(self):
        from django.test import Client
        self.client=Client(enforce_csrf_checks=True)
        svc.bootstrap()
        self.user=get_user_model().objects.create_user('csrf-reviewer',password='test-password')

    def token(self,response):
        import re
        return re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"',response.content.decode()).group(1)

    def test_login_upload_and_review_without_separate_csrf_cookie(self):
        from django.conf import settings
        from .models import BankRow
        response=self.client.get(reverse('login'))
        self.assertNotIn(settings.CSRF_COOKIE_NAME,self.client.cookies)
        response=self.client.post(reverse('login'),{
            'username':'csrf-reviewer','password':'test-password','csrfmiddlewaretoken':self.token(response)})
        self.assertEqual(response.status_code,302)
        response=self.client.get(reverse('imports'))
        response=self.client.post(reverse('imports'),{'kind':'bank','csrfmiddlewaretoken':self.token(response),
            'file':SimpleUploadedFile('bank.csv',b'date,Description,ref,debit,credit\n2026-09-30,Fee,FEE,5,0\n')})
        self.assertRedirects(response,reverse('bank_statement'))
        response=self.client.get(reverse('bank_statement'))
        response=self.client.post(reverse('bank_statement'),{
            'csrfmiddlewaretoken':self.token(response),'token':self.client.session['bank_statement']['token'],
            'bank_account':Account.objects.get(code='1100').pk,
            'line-0-counter_account':Account.objects.get(code='6100').pk})
        self.assertRedirects(response,reverse('bank'))
        self.assertEqual(BankRow.objects.count(),1)
        self.assertNotIn(settings.CSRF_COOKIE_NAME,self.client.cookies)

    def test_missing_invalid_token_and_untrusted_origin_still_rejected(self):
        self.client.force_login(self.user)
        response=self.client.get(reverse('imports'))
        token=self.token(response)
        for data,extra in [({'kind':'bank'},{}),
                           ({'kind':'bank','csrfmiddlewaretoken':'x'*64},{}),
                           ({'kind':'bank','csrfmiddlewaretoken':token},{'HTTP_ORIGIN':'https://untrusted.example'})]:
            with self.subTest(data=data,extra=extra):
                self.assertEqual(self.client.post(reverse('imports'),data,**extra).status_code,403)
        self.assertNotIn('bank_statement',self.client.session)


@override_settings(ALLOWED_HOSTS=['testserver'])
class RecurringFormTests(TestCase):
    def setUp(self):
        svc.bootstrap()
        self.user=get_user_model().objects.create_user('recurring-editor',is_staff=True)
        self.client.force_login(self.user)
        self.expense=Account.objects.get(code='6100')
        self.bank=Account.objects.get(code='1100')

    def payload(self):
        return {'name':'Monthly rent','start_date':'2026-09-01','end_date':'2026-09-30',
                'frequency':'monthly','reference':'RENT','description':'Office rent',
                'lines-TOTAL_FORMS':'2','lines-INITIAL_FORMS':'0',
                'lines-0-account':self.expense.pk,'lines-0-debit':'123.45','lines-0-credit':'',
                'lines-1-account':self.bank.pk,'lines-1-debit':'','lines-1-credit':'123.45'}

    def test_select_accounts_save_and_generate(self):
        response=self.client.get(reverse('recurring'))
        self.assertContains(response,'name="lines-0-account"')
        self.assertNotContains(response,'textarea name="lines"')
        response=self.client.post(reverse('recurring'),self.payload())
        self.assertRedirects(response,reverse('recurring'))
        template=RecurringJV.objects.get()
        self.assertEqual(template.lines,[['6100',12345,0],['1100',0,12345]])
        svc.generate(self.user)
        entry=JournalEntry.objects.get(source='recurring')
        self.assertEqual(entry.lines.get(account=self.expense).debit,12345)
        self.assertEqual(entry.lines.get(account=self.bank).credit,12345)

    def test_validation_preserves_values(self):
        for changes,error in [({'lines-1-credit':'120'},'Total debits and credits'),
                              ({'end_date':'2026-08-01'},'End date before start date'),
                              ({'lines-0-account':'999999'},'Select a valid choice'),
                              ({'lines-0-credit':'1'},'Enter a positive debit or credit')]:
            with self.subTest(changes=changes):
                response=self.client.post(reverse('recurring'),{**self.payload(),**changes})
                self.assertContains(response,error)
                self.assertContains(response,'Monthly rent')
                self.assertFalse(RecurringJV.objects.exists())

    def test_deleted_row_ignored_and_inactive_account_rejected(self):
        data=self.payload()
        data.update({'lines-TOTAL_FORMS':'3','lines-2-account':'999999','lines-2-debit':'5','lines-2-DELETE':'on'})
        self.assertRedirects(self.client.post(reverse('recurring'),data),reverse('recurring'))
        self.assertEqual(len(RecurringJV.objects.get().lines),2)
        self.expense.active=False
        self.expense.save()
        self.assertContains(self.client.post(reverse('recurring'),self.payload()),'Select a valid choice')
        self.assertEqual(RecurringJV.objects.count(),1)

    def test_nonstaff_cannot_create(self):
        self.user.is_staff=False
        self.user.save()
        self.assertEqual(self.client.post(reverse('recurring'),self.payload()).status_code,400)
        self.assertFalse(RecurringJV.objects.exists())
