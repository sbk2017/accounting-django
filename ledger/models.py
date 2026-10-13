from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError

class Account(models.Model):
    TYPES=[(x,x.title()) for x in ('asset','liability','equity','income','cogs','expense')]
    code=models.CharField(max_length=30,unique=True)
    name=models.CharField(max_length=160)
    type=models.CharField(max_length=15,choices=TYPES)
    parent=models.ForeignKey('self',null=True,blank=True,on_delete=models.PROTECT)
    active=models.BooleanField(default=True)
    class Meta: ordering=['code']
    def __str__(self): return f'{self.code} {self.name}'
    def clean(self):
        if self.parent_id==self.pk and self.pk: raise ValidationError('An account cannot parent itself')
        if self.pk and JournalLine.objects.filter(account_id=self.pk).exists() and Account.objects.get(pk=self.pk).type!=self.type: raise ValidationError('Cannot change the type of an account with journal lines')

class JournalEntry(models.Model):
    STATUSES=[('draft','Draft'),('posted','Posted'),('reversed','Reversed')]
    date=models.DateField(db_index=True)
    reference=models.CharField(max_length=100,blank=True)
    description=models.TextField(blank=True)
    source=models.CharField(max_length=30,default='manual')
    source_key=models.CharField(max_length=250,unique=True,null=True,blank=True)
    status=models.CharField(max_length=10,choices=STATUSES,default='draft')
    reverses=models.ForeignKey('self',null=True,blank=True,on_delete=models.PROTECT)
    custom_fields=models.JSONField(default=dict,blank=True)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.SET_NULL)
    created_at=models.DateTimeField(auto_now_add=True)
    posted_at=models.DateTimeField(null=True,blank=True)
    def __str__(self): return f'JV {self.pk} {self.reference}'

class JournalLine(models.Model):
    entry=models.ForeignKey(JournalEntry,related_name='lines',on_delete=models.CASCADE)
    account=models.ForeignKey(Account,on_delete=models.PROTECT)
    debit=models.BigIntegerField(default=0)
    credit=models.BigIntegerField(default=0)
    custom_fields=models.JSONField(default=dict,blank=True)
    def clean(self):
        if self.debit<0 or self.credit<0 or bool(self.debit)==bool(self.credit): raise ValidationError('Exactly one side must be positive')

class LockedPeriod(models.Model):
    month=models.CharField(max_length=7,unique=True)
    def __str__(self):return self.month

class RecurringJV(models.Model):
    FREQUENCIES=[('monthly','Monthly'),('quarterly','Quarterly'),('annual','Annual')]
    name=models.CharField(max_length=160)
    start_date=models.DateField()
    end_date=models.DateField(null=True,blank=True)
    frequency=models.CharField(max_length=10,choices=FREQUENCIES)
    reference=models.CharField(max_length=100,blank=True)
    description=models.TextField(blank=True)
    lines=models.JSONField(default=list)
    active=models.BooleanField(default=True)
    def clean(self):
        if self.end_date and self.end_date<self.start_date:raise ValidationError('End date before start date')

class Occurrence(models.Model):
    template=models.ForeignKey(RecurringJV,on_delete=models.PROTECT)
    period_date=models.DateField()
    entry=models.ForeignKey(JournalEntry,on_delete=models.PROTECT)
    class Meta: constraints=[models.UniqueConstraint(fields=['template','period_date'],name='unique_occurrence')]

class BankRow(models.Model):
    source_key=models.CharField(max_length=250,unique=True)
    date=models.DateField()
    reference=models.CharField(max_length=100,blank=True)
    description=models.TextField(blank=True)
    amount=models.BigIntegerField()
    bank_account=models.ForeignKey(Account,related_name='+',on_delete=models.PROTECT)
    counter_account=models.ForeignKey(Account,related_name='+',on_delete=models.PROTECT)
    status=models.CharField(max_length=12,default='unmatched')
    matched_entry=models.ForeignKey(JournalEntry,null=True,blank=True,related_name='+',on_delete=models.PROTECT)
    draft_entry=models.ForeignKey(JournalEntry,null=True,blank=True,related_name='+',on_delete=models.PROTECT)

class Setting(models.Model):
    key=models.CharField(max_length=80,unique=True)
    value=models.TextField()

class AuditEvent(models.Model):
    created_at=models.DateTimeField(auto_now_add=True)
    user=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.SET_NULL)
    action=models.CharField(max_length=80)
    detail=models.TextField(blank=True)
