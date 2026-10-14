"""Read-only reporting API. All monetary values are integer AED fils."""
from datetime import date
from functools import wraps
import secrets

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Sum
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .models import Account, JournalLine
from . import services


def endpoint(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        token = settings.REPORTING_API_TOKEN
        supplied = request.headers.get('Authorization', '')
        if not token or not secrets.compare_digest(supplied.encode(), ('Bearer ' + token).encode()):
            response = JsonResponse({'error': 'Valid reporting bearer token required'}, status=401)
            response['WWW-Authenticate'] = 'Bearer'
        elif request.method != 'GET':
            response = JsonResponse({'error': 'Only GET is supported'}, status=405)
            response['Allow'] = 'GET'
        else:
            try:
                response = view(request, *args, **kwargs)
            except ValueError as exc:
                response = JsonResponse({'error': str(exc)}, status=400)
        response['Cache-Control'] = 'no-store'
        return response
    return csrf_exempt(wrapped)


def filters(request, allowed=()):
    unknown = set(request.GET) - {'date_from', 'date_to', *allowed}
    if unknown:
        raise ValueError('Unknown query parameters: ' + ', '.join(sorted(unknown)))
    for key in request.GET:
        if len(request.GET.getlist(key)) != 1:
            raise ValueError('Repeated query parameter: ' + key)
    start, end = None, None
    try:
        start = date.fromisoformat(request.GET['date_from']) if 'date_from' in request.GET else None
        end = date.fromisoformat(request.GET['date_to']) if 'date_to' in request.GET else None
        for key, value in [('date_from', start), ('date_to', end)]:
            if value and request.GET[key] != value.isoformat():
                raise ValueError()
    except ValueError:
        raise ValueError('Dates must use YYYY-MM-DD')
    if start and end and start > end:
        raise ValueError('date_from must not exceed date_to')
    return start, end


def posted():
    return JournalLine.objects.filter(entry__status__in=('posted', 'reversed'))


def period_lines(start, end):
    lines = posted()
    if start:
        lines = lines.filter(entry__date__gte=start)
    if end:
        lines = lines.filter(entry__date__lte=end)
    return lines


def totals(lines):
    result = lines.aggregate(debit=Sum('debit'), credit=Sum('credit'))
    debit, credit = result['debit'] or 0, result['credit'] or 0
    return {'debit': debit, 'credit': credit, 'balance': debit - credit}


def metadata(start=None, end=None):
    return {'currency': 'AED', 'amount_unit': 'fils', 'date_from': start, 'date_to': end,
            'included_statuses': ['posted', 'reversed']}


def paginate(request, queryset):
    try:
        page = int(request.GET.get('page', '1'))
        size = int(request.GET.get('page_size', '100'))
    except ValueError:
        raise ValueError('page and page_size must be integers')
    if page < 1 or not 1 <= size <= 500:
        raise ValueError('page must be positive; page_size must be between 1 and 500')
    paginator = Paginator(queryset, size)
    if page > paginator.num_pages:
        raise ValueError('Page out of range')
    return paginator.page(page), {'page': page, 'page_size': size, 'count': paginator.count,
                                  'next_page': page + 1 if page < paginator.num_pages else None}


@endpoint
def accounts(request):
    filters(request, ('page', 'page_size'))
    if {'date_from', 'date_to'} & set(request.GET):
        raise ValueError('Accounts accepts only page and page_size')
    page, paging = paginate(request, Account.objects.values('code', 'name', 'type', 'active', 'parent__code'))
    return JsonResponse({**metadata(), **paging, 'results': list(page)})


@endpoint
def reports(request):
    start, end = filters(request)
    lines = period_lines(start, end)
    rows = list(lines.values('account__code', 'account__name', 'account__type').annotate(
        debit=Sum('debit'), credit=Sum('credit')).order_by('account__code'))
    trial = [{'code': r['account__code'], 'name': r['account__name'], 'type': r['account__type'],
              'debit': r['debit'], 'credit': r['credit'], 'balance': r['debit'] - r['credit']} for r in rows]
    amounts = {typ: sum(r['balance'] for r in trial if r['type'] == typ)
               for typ in ('income', 'cogs', 'expense')}
    sales, cogs, expenses = -amounts['income'], amounts['cogs'], amounts['expense']
    cumulative = period_lines(None, end).values('account__code', 'account__name', 'account__type').annotate(
        debit=Sum('debit'), credit=Sum('credit')).order_by('account__code')
    balance_sheet = []
    earnings = 0
    for row in cumulative:
        typ = row['account__type']
        net = row['debit'] - row['credit']
        if typ in ('asset', 'liability', 'equity'):
            balance_sheet.append({'code': row['account__code'], 'name': row['account__name'],
                                  'type': typ, 'balance': net if typ == 'asset' else -net})
        else:
            earnings -= net
    output_vat = -sum(r['balance'] for r in trial if r['code'] == services.setting('output_vat'))
    input_vat = sum(r['balance'] for r in trial if r['code'] == services.setting('input_vat'))
    return JsonResponse({**metadata(start, end), 'trial_balance': {'rows': trial, 'totals': totals(lines)},
                         'profit_and_loss': {'sales': sales, 'cogs': cogs, 'gross_profit': sales - cogs,
                                             'expenses': expenses, 'net_profit': sales - cogs - expenses},
                         'vat': {'output': output_vat, 'input': input_vat, 'net': output_vat - input_vat},
                         'balance_sheet': {'rows': balance_sheet, 'earnings': earnings, 'as_of': end}})


@endpoint
def ledger(request, code):
    start, end = filters(request, ('page', 'page_size', 'source'))
    account = Account.objects.filter(code=code).values('code', 'name', 'type', 'active').first()
    if account is None:
        return JsonResponse({'error': 'Account not found'}, status=404)
    base = posted().filter(account__code=code)
    source = request.GET.get('source')
    if source is not None:
        base = base.filter(entry__source=source)
    opening = totals(base.filter(entry__date__lt=start))['balance'] if start else 0
    lines = base
    if start:
        lines = lines.filter(entry__date__gte=start)
    if end:
        lines = lines.filter(entry__date__lte=end)
    lines = lines.select_related('entry').order_by('entry__date', 'entry_id', 'pk')
    page, paging = paginate(request, lines)
    period = totals(lines)
    # Include preceding pages in the running balance without a SQL window over a sliced query.
    preceding = lines.values_list('pk', flat=True)[:page.start_index() - 1] if paging['count'] else []
    running = opening + totals(JournalLine.objects.filter(pk__in=preceding))['balance']
    rows = []
    for line in page:
        entry = line.entry
        running += line.debit - line.credit
        rows.append({'line_id': line.pk, 'journal_id': entry.pk, 'date': entry.date,
                     'reference': entry.reference, 'description': entry.description,
                     'source': entry.source, 'source_key': entry.source_key, 'status': entry.status,
                     'reverses_id': entry.reverses_id, 'journal_custom_fields': entry.custom_fields,
                     'line_custom_fields': line.custom_fields, 'debit': line.debit,
                     'credit': line.credit, 'balance': running})
    return JsonResponse({**metadata(start, end), **paging, 'account': account, 'source': source,
                         'opening_balance': opening, 'period_totals': period,
                         'closing_balance': opening + period['balance'], 'results': rows})
