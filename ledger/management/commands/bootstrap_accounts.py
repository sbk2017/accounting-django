from django.core.management.base import BaseCommand
from ledger.services import bootstrap
class Command(BaseCommand):
    help='Create sample chart of accounts and default settings if missing'
    def handle(self,*args,**kwargs):bootstrap();self.stdout.write('Accounting defaults ready')
