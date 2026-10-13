import json,logging
from uuid import uuid4
from datetime import date,timedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required,user_passes_test
from django.db import transaction
from django.db.models import Sum, Value
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.http import HttpResponse,HttpResponseBadRequest
from django.shortcuts import get_object_or_404,redirect,render
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import Account,JournalEntry,JournalLine,LockedPeriod,RecurringJV,Occurrence,BankRow,Setting,AuditEvent
from . import services as svc
from .forms import JournalEntryForm, JournalLineFormSet, BankStatementForm, BankStatementLineForm, RecurringJVForm
log=logging.getLogger(__name__)

def money(n):return f'{(n or 0)/100:,.2f}'
def staff(u):return u.is_staff

def attempt(action,func,success):
    try:
        result=func();messages.success(action,success(result))
    except Exception as exc:
        log.exception('Action %s failed',action)
        messages.error(action,f'{type(exc).__name__}: {exc}')
    return redirect(action.META.get('HTTP_REFERER','/'))

@login_required
def dashboard(request):
    today=timezone.localdate()
    month_start=today.replace(day=1)
    last_month_end=month_start-timedelta(days=1)
    periods=[]
    for label,start,end in [('Current month',month_start,today),
                            ('Last month',last_month_end.replace(day=1),last_month_end),
                            ('Year to date',today.replace(month=1,day=1),today)]:
        totals=JournalLine.objects.filter(entry__status__in=('posted','reversed'),
            entry__date__range=(start,end),account__type__in=('income','cogs','expense')
        ).values('account__type').annotate(dr=Sum('debit'),cr=Sum('credit'))
        amounts={'income':0,'cogs':0,'expense':0}
        for row in totals:
            typ=row['account__type']
            amounts[typ]=row['cr']-row['dr'] if typ=='income' else row['dr']-row['cr']
        periods.append({'label':label,'start':start,'end':end,
            'sales':money(amounts['income']),'gross':money(amounts['income']-amounts['cogs']),
            'expenses':money(amounts['expense']),'profit':money(amounts['income']-amounts['cogs']-amounts['expense'])})
    return render(request,'ledger/dashboard.html',{'periods':periods,
        'drafts':JournalEntry.objects.filter(status='draft').count(),
        'bank_count':BankRow.objects.filter(status='unmatched').count()})

@login_required
def accounts(request):
    if request.method=='GET' and request.GET.get('download')=='csv':
        response=HttpResponse(svc.export_accounts(),content_type='text/csv; charset=utf-8')
        response['Content-Disposition']='attachment; filename="chart-of-accounts.csv"'
        return response
    if request.method=='POST':
        if not request.user.is_staff:return HttpResponseBadRequest('Staff permission required')
        if request.POST.get('action')=='import_csv':
            def restore():
                upload=request.FILES.get('file')
                if not upload:raise ValueError('Choose a chart of accounts CSV file')
                return svc.import_accounts(upload,request.user)
            return attempt(request,restore,lambda result:f'{result[0]} accounts created; {result[1]} updated')
        def save():
            p=request.POST; parent=Account.objects.get(code=p['parent']) if p.get('parent') else None
            a=Account.objects.filter(pk=p.get('id')).first() if p.get('id') else Account()
            a.code=p['code'].strip();a.name=p['name'].strip();a.type=p['type'];a.parent=parent;a.active=p.get('active')=='on';a.full_clean();a.save();svc.audit('account_saved',str(a),request.user)
        return attempt(request,save,lambda _: 'Account saved')
    return render(request,'ledger/accounts.html',{'accounts':Account.objects.select_related('parent'),'types':Account.TYPES})

@login_required
def journals(request):
    entry_form = JournalEntryForm(request.POST if request.method == 'POST' else None,
                                  initial={'date': timezone.localdate()})
    line_forms = JournalLineFormSet(request.POST if request.method == 'POST' else None, prefix='lines')
    if request.method=='POST':
        valid_entry = entry_form.is_valid()
        valid_lines = line_forms.is_valid()
        if valid_entry and valid_lines:
            values = entry_form.cleaned_data
            rows = [(f.cleaned_data['account'].code, int(f.cleaned_data['debit'] * 100),
                     int(f.cleaned_data['credit'] * 100)) for f in line_forms
                    if f.cleaned_data and not f.cleaned_data.get('DELETE')]
            try:
                entry = svc.create_entry(values['date'], values['reference'], values['description'],
                                         'manual', rows, request.user)
            except Exception as exc:
                log.exception('Journal creation failed user=%s', request.user.pk)
                entry_form.add_error(None, str(exc))
            else:
                messages.success(request, f'Draft JV {entry.pk} created')
                return redirect('journal_detail', pk=entry.pk)

    entries = JournalEntry.objects.annotate(amount=Coalesce(Sum('lines__debit'), Value(0)))
    filters = {key: request.GET.get(key, '').strip() for key in
               ('id', 'date_from', 'date_to', 'reference', 'description', 'source', 'status', 'amount_min', 'amount_max')}
    filter_errors = []
    for key, lookup in [('id', 'pk'), ('date_from', 'date__gte'), ('date_to', 'date__lte'),
                        ('amount_min', 'amount__gte'), ('amount_max', 'amount__lte')]:
        if not filters[key]:
            continue
        try:
            if key == 'id':
                value = int(filters[key])
                if value < 1: raise ValueError('ID must be positive')
            elif key.startswith('date_'):
                value = date.fromisoformat(filters[key])
            else:
                from decimal import Decimal, InvalidOperation
                amount = Decimal(filters[key])
                if not amount.is_finite() or amount < 0 or amount * 100 != (amount * 100).to_integral_value():
                    raise ValueError('Use a nonnegative amount with at most two decimals')
                value = int(amount * 100)
            entries = entries.filter(**{lookup: value})
        except (ValueError, ArithmeticError):
            filter_errors.append(f'Invalid {key.replace("_", " ")}.')
    for key in ('reference', 'description', 'source'):
        if filters[key]: entries = entries.filter(**{key + '__icontains': filters[key]})
    if filters['status']:
        if filters['status'] in dict(JournalEntry.STATUSES):
            entries = entries.filter(status=filters['status'])
        else:
            filter_errors.append('Invalid status.')
    if filter_errors: entries = entries.none()
    paginator = Paginator(entries.order_by('-pk'), 20)
    page = paginator.get_page(request.GET.get('page'))
    for entry in page: entry.amount_formatted = money(entry.amount)
    query = request.GET.copy()
    query.pop('page', None)
    return render(request, 'ledger/journals.html', {'entries': page, 'page_obj': page,
                   'filter_query': query.urlencode(), 'filters': filters, 'filter_errors': filter_errors,
                   'entry_form': entry_form, 'line_forms': line_forms, 'statuses': JournalEntry.STATUSES})


@login_required
@require_POST
def post_selected_journals(request):
    if not request.user.is_staff:
        return HttpResponseBadRequest('Staff permission required')
    selected = request.POST.getlist('selected')
    try:
        ids = sorted(set(int(value) for value in selected))
        if not ids or len(ids) > 20 or any(pk < 1 for pk in ids):
            raise ValueError('Select between 1 and 20 draft vouchers on this page.')
        # All selected vouchers post together, or none do if one fails validation.
        with transaction.atomic():
            for pk in ids:
                try:
                    svc.post(pk, request.user)
                except Exception as exc:
                    raise ValueError(f'JV {pk}: {exc}') from exc
    except Exception as exc:
        log.warning('Bulk posting rejected user=%s: %s', request.user.pk, exc)
        messages.error(request, f'No vouchers posted. {exc}')
    else:
        messages.success(request, f'{len(ids)} voucher(s) posted.')
    # Retain only the query string, never a client-supplied redirect destination.
    from django.urls import reverse
    from urllib.parse import parse_qsl, urlencode
    allowed = {'id', 'date_from', 'date_to', 'reference', 'description', 'source', 'status', 'amount_min', 'amount_max', 'page'}
    query = urlencode([(k, v) for k, v in parse_qsl(request.POST.get('return_query', '')) if k in allowed])
    return redirect(reverse('journals') + ('?' + query if query else ''))

@login_required
def journal_detail(request,pk):return render(request,'ledger/detail.html',{'entry':get_object_or_404(JournalEntry,pk=pk),'lines':[{'account':l.account,'debit':money(l.debit),'credit':money(l.credit)} for l in JournalLine.objects.filter(entry_id=pk).select_related('account')]})
@login_required
@require_POST
def post_journal(request,pk):
    if not request.user.is_staff:return HttpResponseBadRequest('Staff permission required')
    return attempt(request,lambda:svc.post(pk,request.user),lambda e:f'JV {e.pk} posted')
@login_required
@require_POST
def reverse_journal(request,pk):
    if not request.user.is_staff:return HttpResponseBadRequest('Staff permission required')
    return attempt(request,lambda:svc.reverse(pk,request.user),lambda e:f'Reversal JV {e.pk} posted')

@login_required
def recurring(request):
    if request.method=='POST' and not request.user.is_staff:
        return HttpResponseBadRequest('Staff permission required')
    data=request.POST if request.method=='POST' else None
    form=RecurringJVForm(data,initial={'start_date':timezone.localdate(),'frequency':'monthly'})
    line_forms=JournalLineFormSet(data,prefix='lines')
    if request.method=='POST':
        valid_form=form.is_valid()
        valid_lines=line_forms.is_valid()
        if valid_form and valid_lines:
            with transaction.atomic():
                template=form.save(commit=False)
                template.lines=[(f.cleaned_data['account'].code,int(f.cleaned_data['debit']*100),
                                 int(f.cleaned_data['credit']*100)) for f in line_forms
                                if f.cleaned_data and not f.cleaned_data.get('DELETE')]
                template.save()
                svc.audit('recurring_created',template.name,request.user)
            messages.success(request,'Recurring JV created')
            return redirect('recurring')
    return render(request,'ledger/recurring.html',{'templates':RecurringJV.objects.order_by('-pk'),
                  'form':form,'line_forms':line_forms})
@login_required
@require_POST
def generate_due(request):
    if not request.user.is_staff:return HttpResponseBadRequest('Staff permission required')
    return attempt(request,lambda:svc.generate(request.user),lambda n:f'{n} drafts generated')
@login_required
@require_POST
def toggle_recurring(request,pk):
    if not request.user.is_staff:return HttpResponseBadRequest('Staff permission required')
    def toggle():
        r=get_object_or_404(RecurringJV,pk=pk);r.active=not r.active;r.save(update_fields=['active']);svc.audit('recurring_toggled',str(r.pk),request.user)
    return attempt(request,toggle,lambda _: 'Status changed')

@login_required
def imports(request):
    if request.method=='POST':
        kind=request.POST.get('kind')
        if kind=='expense':return attempt(request,lambda:svc.import_expenses(request.FILES['file'],request.user),lambda x:f'{x[0]} expense drafts created; {x[1]} duplicates skipped')
        if kind=='bank':
            try:
                upload=request.FILES.get('file')
                if upload is None:
                    raise ValueError('Choose a statement CSV file')
                statement=svc.preview_bank_statement(upload)
                statement['token']=uuid4().hex
                request.session['bank_statement']=statement
                return redirect('bank_statement')
            except (ValueError,UnicodeError) as exc:
                messages.error(request,str(exc))
                return redirect('imports')
        if kind=='dolibarr':return attempt(request,lambda:svc.sync_dolibarr(date.fromisoformat(request.POST['since']) if request.POST.get('since') else None,request.user),lambda x:f'{x[0]} invoice drafts created; {x[1]} skipped')
        return HttpResponseBadRequest('Unknown import type')
    return render(request,'ledger/imports.html')

@login_required
def bank_statement(request):
    statement=request.session.get('bank_statement')
    if not statement:
        messages.error(request,'Upload a bank statement first.')
        return redirect('imports')
    data=request.POST if request.method=='POST' else None
    form=BankStatementForm(data)
    rows=[]
    for i,row in enumerate(statement['rows']):
        rows.append({**row,'debit_display':money(row['debit']),'credit_display':money(row['credit']),
                     'form':BankStatementLineForm(data,prefix=f'line-{i}')})
    if request.method=='POST':
        if request.POST.get('token')!=statement['token']:
            messages.error(request,'This review was replaced by another upload. Review the current statement before saving.')
        else:
            valid=form.is_valid()
            for row in rows:
                line_form=row['form']
                if not line_form.is_valid():
                    valid=False
                elif form.cleaned_data.get('bank_account')==line_form.cleaned_data['counter_account']:
                    line_form.add_error('counter_account','Choose an account different from the bank account.')
                    valid=False
            if valid:
                count,skipped=svc.save_bank_statement(statement,form.cleaned_data['bank_account'],
                    [row['form'].cleaned_data['counter_account'] for row in rows],request.user)
                del request.session['bank_statement']
                messages.success(request,f'{count} bank rows staged; {skipped} duplicates skipped.')
                return redirect('bank')
    return render(request,'ledger/bank_statement.html',{'form':form,'rows':rows,'token':statement['token']})


@login_required
def bank(request):return render(request,'ledger/bank.html',{'rows':[{'pk':r.pk,'date':r.date,'reference':r.reference,'description':r.description,'debit':money(max(-r.amount,0)),'credit':money(max(r.amount,0)),'bank_account':r.bank_account,'counter_account':r.counter_account,'status':r.status} for r in BankRow.objects.select_related('bank_account','counter_account').order_by('-pk')[:150]]})
@login_required
@require_POST
def bank_draft(request,pk):
    def save():
        with transaction.atomic():
            r=get_object_or_404(BankRow.objects.select_for_update(),pk=pk)
            if r.status!='unmatched':raise ValueError('Row already handled')
            n=abs(r.amount);lines=[(r.bank_account.code,n,0),(r.counter_account.code,0,n)] if r.amount>0 else [(r.counter_account.code,n,0),(r.bank_account.code,0,n)]
            e=svc.create_entry(r.date,r.reference,r.description,'csv_bank',lines,request.user,r.source_key)
            r.draft_entry=e;r.status='drafted';r.save(update_fields=['draft_entry','status']);return e
    return attempt(request,save,lambda e:f'Bank draft JV {e.pk} created')
@login_required
@require_POST
def bank_match(request,pk):
    def save():
        with transaction.atomic():
            r=get_object_or_404(BankRow.objects.select_for_update(),pk=pk)
            if r.status!='unmatched':raise ValueError('Row already handled')
            e=JournalEntry.objects.get(pk=int(request.POST['entry_id']),status='posted')
            if not e.lines.filter(account=r.bank_account,debit=abs(r.amount) if r.amount>0 else 0,credit=abs(r.amount) if r.amount<0 else 0).exists():raise ValueError('JV bank line does not match amount')
            r.status='matched';r.matched_entry=e;r.save(update_fields=['status','matched_entry']);svc.audit('bank_matched',f'{r.pk} to {e.pk}',request.user)
    return attempt(request,save,lambda _: 'Bank row matched')

def summary(period):
    q=JournalLine.objects.filter(entry__status__in=('posted','reversed'),entry__date__startswith=period).values('account__code','account__name','account__type').annotate(dr=Sum('debit'),cr=Sum('credit')).order_by('account__code')
    return [{'code':r['account__code'],'name':r['account__name'],'type':r['account__type'],'dr':r['dr'],'cr':r['cr'],'normal':r['cr']-r['dr'] if r['account__type']=='income' else r['dr']-r['cr']} for r in q]
@login_required
def reports(request):
    year=request.GET.get('year',str(timezone.localdate().year));month=request.GET.get('month','')
    try:
        y=int(year);m=int(month) if month else None
        if y<1900 or y>9999 or m is not None and not 1<=m<=12:raise ValueError()
    except ValueError:return HttpResponseBadRequest('Invalid period')
    period=f'{y:04d}'+(f'-{m:02d}' if m else '');rows=summary(period)
    sums={k:sum(r['normal'] for r in rows if r['type']==k) for k in ('income','cogs','expense')}
    vatout=sum(r['cr']-r['dr'] for r in rows if r['code']==svc.setting('output_vat'))
    vatin=sum(r['dr']-r['cr'] for r in rows if r['code']==svc.setting('input_vat'))
    end=date(y,m or 12,calendar_last(y,m or 12))
    bal=JournalLine.objects.filter(entry__status__in=('posted','reversed'),entry__date__lte=end).values('account__code','account__name','account__type').annotate(dr=Sum('debit'),cr=Sum('credit'))
    balance=[{'name':r['account__code']+' '+r['account__name'],'value':money(r['dr']-r['cr'] if r['account__type']=='asset' else r['cr']-r['dr'])} for r in bal if r['account__type'] in ('asset','liability','equity')]
    earnings=sum((r['cr']-r['dr'] if r['account__type']=='income' else r['dr']-r['cr'])* (1 if r['account__type']=='income' else -1) for r in bal if r['account__type'] in ('income','cogs','expense'))
    balance.append({'name':'Current and prior earnings','value':money(earnings)})
    for r in rows:r.update(dr_fmt=money(r['dr']),cr_fmt=money(r['cr']),normal_fmt=money(r['normal']))
    return render(request,'ledger/reports.html',{'year':year,'month':month,'period':period,'rows':rows,'sales':money(sums['income']),'cogs':money(sums['cogs']),'gross':money(sums['income']-sums['cogs']),'expenses':money(sums['expense']),'profit':money(sums['income']-sums['cogs']-sums['expense']),'vatout':money(vatout),'vatin':money(vatin),'vatnet':money(vatout-vatin),'balance':balance})
def calendar_last(y,m):
    import calendar
    return calendar.monthrange(y,m)[1]
@login_required
def ledger(request,code):
    account=get_object_or_404(Account,code=code)
    lines=JournalLine.objects.filter(account=account,entry__status__in=('posted','reversed')).select_related('entry').order_by('entry__date','entry_id')[:1000]
    balance=0;out=[]
    for l in lines:
        balance+=l.debit-l.credit;out.append({'entry':l.entry,'debit':money(l.debit),'credit':money(l.credit),'balance':money(balance)})
    return render(request,'ledger/ledger.html',{'account':account,'rows':out})
@login_required
def settings_view(request):
    if request.method=='POST':
        if not request.user.is_staff:return HttpResponseBadRequest('Staff permission required')
        if request.POST.get('kind')=='lock':
            def lock():
                m=request.POST['month'];date.fromisoformat(m+'-01');LockedPeriod.objects.get_or_create(month=m);svc.audit('month_locked',m,request.user)
            return attempt(request,lock,lambda _: 'Month locked')
        def save():
            for key in svc.DEFAULTS:
                if key in request.POST:
                    value=request.POST[key]
                    if key in ('receivables','sales','output_vat','input_vat','payables'):Account.objects.get(code=value,active=True)
                    if key=='custom_definitions' and not isinstance(json.loads(value),list):raise ValueError('Custom definitions must be a JSON list')
                    Setting.objects.update_or_create(key=key,defaults={'value':value})
            svc.audit('settings_updated','Mappings updated',request.user)
        return attempt(request,save,lambda _: 'Settings saved')
    return render(request,'ledger/settings.html',{'settings':Setting.objects.order_by('key'),'periods':LockedPeriod.objects.order_by('-month')})
