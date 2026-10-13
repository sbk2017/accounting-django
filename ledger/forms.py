from decimal import Decimal

from django import forms
from django.forms import BaseFormSet, formset_factory

from .models import Account, RecurringJV


class JournalEntryForm(forms.Form):
    date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))
    reference = forms.CharField(max_length=100, required=False)
    description = forms.CharField(required=False, widget=forms.TextInput())


class JournalLineForm(forms.Form):
    account = forms.ModelChoiceField(queryset=Account.objects.filter(active=True), empty_label='Select an account')
    debit = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False,
                               widget=forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'placeholder': '0.00'}))
    credit = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False,
                                widget=forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'placeholder': '0.00'}))

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        debit = cleaned.get('debit') or Decimal('0')
        credit = cleaned.get('credit') or Decimal('0')
        if bool(debit) == bool(credit):
            raise forms.ValidationError('Enter a positive debit or credit, with the other side blank or zero.')
        cleaned.update(debit=debit, credit=credit)
        return cleaned


class BalancedJournalLines(BaseFormSet):
    def clean(self):
        super().clean()
        rows = [f for f in self.forms if f.cleaned_data and not f.cleaned_data.get('DELETE')]
        if any(f.errors for f in rows):
            return
        if len(rows) < 2:
            raise forms.ValidationError('Add at least two journal lines.')
        debit = sum((f.cleaned_data['debit'] for f in rows), Decimal('0'))
        credit = sum((f.cleaned_data['credit'] for f in rows), Decimal('0'))
        if debit <= 0 or debit != credit:
            raise forms.ValidationError('Total debits and credits must be equal and greater than zero.')


JournalLineFormSet = formset_factory(JournalLineForm, formset=BalancedJournalLines,
                                   extra=2, can_delete=True, max_num=200, validate_max=True)


class BankStatementForm(forms.Form):
    bank_account = forms.ModelChoiceField(
        queryset=Account.objects.filter(active=True), empty_label='Select the bank account')


class BankStatementLineForm(forms.Form):
    counter_account = forms.ModelChoiceField(
        queryset=Account.objects.filter(active=True), empty_label='Select the opposite account')


class RecurringJVForm(forms.ModelForm):
    class Meta:
        model = RecurringJV
        fields = ['name', 'start_date', 'end_date', 'frequency', 'reference', 'description']
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'end_date': forms.DateInput(attrs={'type': 'date'}),
            'description': forms.TextInput(),
        }
