from django import forms
from models import Route, CycleTrip


class RouteForm(forms.ModelForm):
    class Meta:
        model = Route
        fields = ('distance', 'description', 'gpx_file', 'ascent', 'descent', 'route_url')

class EventForm(forms.ModelForm):
    class Meta:
        model = CycleTrip
        fields = ('route', 'date', 'time', 'comment', 'sensor_file', 'kcal', 'url')

class CycleTripForm(EventForm):
    pass

class FullCycleTripForm(forms.ModelForm):
    class Meta:
        model = CycleTrip
        exclude = ('user', 'content_type', 'object_id')
