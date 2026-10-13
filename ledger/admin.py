from django.contrib import admin
from .models import Account,JournalEntry,JournalLine,LockedPeriod,RecurringJV,Occurrence,BankRow,Setting,AuditEvent
@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display=('id','code','name','type','parent','active')
    search_fields=('code','name')
class LineInline(admin.TabularInline):
    model=JournalLine
    extra=0
    readonly_fields=('account','debit','credit','custom_fields')
    can_delete=False
    def has_add_permission(self,request,obj=None):return False
@admin.register(JournalEntry)
class EntryAdmin(admin.ModelAdmin):
    list_display=('id','date','reference','source','status','created_by')
    list_filter=('status','source')
    inlines=[LineInline]
    readonly_fields=('date','reference','description','source','source_key','status','reverses','custom_fields','created_by','posted_at','created_at')
    def has_add_permission(self,request):return False
    def has_delete_permission(self,request,obj=None):return False
for model in (LockedPeriod,RecurringJV,Occurrence,BankRow,Setting,AuditEvent):
    admin.site.register(model)
