from django import forms
from django.db.models import Q


class LimitedChoices:
    def optgroups(self, name, value, attrs=None):
        original = self.choices
        if hasattr(original, "queryset"):
            qs = original.queryset
            selected = [v for v in value if str(v).isascii() and str(v).isdigit()]
            bounded = qs.filter(Q(pk__in=qs.values("pk")[:50]) | Q(pk__in=selected))
            self.choices = [("", "—")] if not self.allow_multiple_selected else []
            self.choices = list(self.choices) + [
                (obj.pk, original.field.label_from_instance(obj)) for obj in bounded
            ]
        try:
            return super().optgroups(name, value, attrs)
        finally:
            self.choices = original


class SearchSelect(LimitedChoices, forms.Select):
    pass


class SearchSelectMultiple(LimitedChoices, forms.SelectMultiple):
    pass
