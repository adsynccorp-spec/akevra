"""Scheduling API: appointments (logistics only) and pre-supervision preparation notes."""

from django.core.exceptions import ValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import write_audit
from apps.core.api_serializers import (
    JSON_RESPONSE,
    AppointmentCancelSerializer,
    AppointmentCreateSerializer,
    AppointmentPatchSerializer,
    PrepNoteSerializer,
)
from apps.rbac.permissions import HasWorkspace
from apps.rbac.services import has_permission
from apps.supervision import notifications, scheduling
from apps.supervision.models import Appointment
from apps.supervision.monthly_views import (
    _bad_request,
    _denied,
    _invalid,
    _not_found,
    _relationship,
    _relationship_summary,
    _relationships_with,
)


def _appointments():
    return (
        Appointment.objects.filter(record_status="active")
        .select_related("relationship__supervisor", "relationship__supervisee", "created_by")
        .prefetch_related("prep_notes__submitted_by", "session_records", "events__actor")
    )


def _appointment(pk):
    return _appointments().filter(pk=pk).first()


def _with_rights(request, appointment, rel, *, detail=False):
    me = request.user_account
    return {
        **scheduling.serialize_appointment(appointment, detail=detail),
        "can_manage": has_permission(me, "appointment.manage", relationship=rel),
        "can_confirm": has_permission(me, "appointment.confirm", relationship=rel),
        "can_prepare": has_permission(me, "prep.submit", relationship=rel),
        "can_document": has_permission(me, "session.document", relationship=rel),
    }


class AppointmentListView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Scheduling"], summary="Appointments of a relationship", operation_id="appointments_list", responses=JSON_RESPONSE)
    def get(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "session.view", rel, "appointment", "list"):
            return denied
        rows = _appointments().filter(relationship=rel).order_by("starts_at")
        return Response({
            "can_manage": has_permission(request.user_account, "appointment.manage", relationship=rel),
            "appointments": [_with_rights(request, a, rel) for a in rows],
        })

    @extend_schema(tags=["Scheduling"], summary="Schedule an appointment, or a weekly batch (Supervisor)", request=AppointmentCreateSerializer, operation_id="appointments_create", responses=JSON_RESPONSE)
    def post(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "appointment.manage", rel, "appointment", "new"):
            return denied
        serializer = AppointmentCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _invalid(serializer)
        try:
            created = scheduling.schedule_appointments(rel, request.user_account, **serializer.validated_data)
        except ValidationError as exc:
            return _bad_request(exc)
        for appointment in created:
            write_audit(action="appointment.schedule", entity_type="appointment", entity_id=appointment.id, request=request,
                        after={"starts_at": appointment.starts_at.isoformat(), "series_id": str(appointment.series_id or "")})
        ordered = sorted(created, key=lambda a: a.starts_at)
        if ordered:
            scheduling.attach_waiting_prep(ordered[0])
        notifications.appointments_scheduled(ordered, request.user_account)
        rows = _appointments().filter(pk__in=[a.pk for a in created]).order_by("starts_at")
        return Response({"appointments": [_with_rights(request, a, rel) for a in rows]}, status=status.HTTP_201_CREATED)


class AllAppointmentsView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Scheduling"], summary="Appointments across all of my relationships", operation_id="appointments_all", responses=JSON_RESPONSE)
    def get(self, request):
        me = request.user_account
        rels = {rel.id: rel for rel in _relationships_with(request, "session.view")}
        rows = _appointments().filter(relationship_id__in=rels).order_by("starts_at")
        return Response({
            "schedulable_relationships": [
                _relationship_summary(rel) for rel in rels.values() if has_permission(me, "appointment.manage", relationship=rel)
            ],
            "appointments": [
                {**_with_rights(request, a, rels[a.relationship_id]), "relationship": _relationship_summary(rels[a.relationship_id])}
                for a in rows
            ],
        })


class AppointmentDetailView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Scheduling"], summary="One appointment with its full scheduling history", operation_id="appointments_retrieve", responses=JSON_RESPONSE)
    def get(self, request, pk):
        appointment = _appointment(pk)
        if appointment is None:
            return _not_found()
        rel = appointment.relationship
        if denied := _denied(request, "session.view", rel, "appointment", appointment.id):
            return denied
        return Response(_with_rights(request, appointment, rel, detail=True))

    @extend_schema(tags=["Scheduling"], summary="Reschedule or update an appointment (Supervisor)", request=AppointmentPatchSerializer, operation_id="appointments_update", responses=JSON_RESPONSE)
    def patch(self, request, pk):
        appointment = _appointment(pk)
        if appointment is None:
            return _not_found()
        rel = appointment.relationship
        if denied := _denied(request, "appointment.manage", rel, "appointment", appointment.id):
            return denied
        serializer = AppointmentPatchSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return _invalid(serializer)
        before = appointment.starts_at.isoformat()
        try:
            scheduling.reschedule(appointment, request.user_account, **serializer.validated_data)
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="appointment.reschedule", entity_type="appointment", entity_id=appointment.id, request=request,
                    before={"starts_at": before}, after={"starts_at": appointment.starts_at.isoformat()})
        if before != appointment.starts_at.isoformat():
            notifications.appointment_changed(appointment, request.user_account, "rescheduled")
        return Response(_with_rights(request, _appointment(pk), rel, detail=True))


class _AppointmentAction(APIView):
    permission_classes = [HasWorkspace]
    permission = "appointment.manage"
    action = None

    def run(self, appointment, request):
        raise NotImplementedError

    def post(self, request, pk):
        appointment = _appointment(pk)
        if appointment is None:
            return _not_found()
        rel = appointment.relationship
        if denied := _denied(request, self.permission, rel, "appointment", appointment.id):
            return denied
        before = appointment.status
        try:
            self.run(appointment, request)
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action=f"appointment.{self.action}", entity_type="appointment", entity_id=appointment.id,
                    request=request, before={"status": before}, after={"status": appointment.status})
        notifications.appointment_changed(appointment, request.user_account, appointment.status)
        return Response(_with_rights(request, _appointment(pk), rel, detail=True))


class AppointmentConfirmView(_AppointmentAction):
    permission = "appointment.confirm"
    action = "confirm"

    @extend_schema(tags=["Scheduling"], summary="Confirm an appointment (either party)", request=None, operation_id="appointments_confirm", responses=JSON_RESPONSE)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, appointment, request):
        scheduling.confirm(appointment, request.user_account)


class AppointmentCancelView(_AppointmentAction):
    action = "cancel"

    @extend_schema(tags=["Scheduling"], summary="Cancel an appointment with a reason (Supervisor)", request=AppointmentCancelSerializer, operation_id="appointments_cancel", responses=JSON_RESPONSE)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, appointment, request):
        scheduling.cancel(appointment, request.user_account, reason=request.data.get("reason") or "")


class AppointmentCompleteView(_AppointmentAction):
    action = "complete"

    @extend_schema(tags=["Scheduling"], summary="Mark an appointment held (counts for nothing until a session is documented)", request=None, operation_id="appointments_complete", responses=JSON_RESPONSE)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, appointment, request):
        scheduling.complete(appointment, request.user_account)


class AppointmentNoShowView(_AppointmentAction):
    action = "no_show"

    @extend_schema(tags=["Scheduling"], summary="Mark an appointment as a no-show (Supervisor)", request=None, operation_id="appointments_no_show", responses=JSON_RESPONSE)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, appointment, request):
        scheduling.mark_no_show(appointment, request.user_account)


class PrepNoteView(APIView):
    """The Supervisee's preparation for an appointment. Never compliance-bearing."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Scheduling"], summary="Save my preparation notes for an appointment (Supervisee)", request=PrepNoteSerializer, operation_id="prep_note_save", responses=JSON_RESPONSE)
    def put(self, request, pk):
        appointment = _appointment(pk)
        if appointment is None:
            return _not_found()
        rel = appointment.relationship
        if denied := _denied(request, "prep.submit", rel, "pre_supervision_note", appointment.id):
            return denied
        serializer = PrepNoteSerializer(data=request.data)
        if not serializer.is_valid():
            return _invalid(serializer)
        first = not appointment.prep_notes.filter(submitted_by=request.user_account, record_status="active").exists()
        try:
            note = scheduling.save_prep_note(appointment, request.user_account, body=serializer.validated_data.get("body", ""),
                                             answers=serializer.validated_data.get("answers"))
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="prep_note.save" if first else "prep_note.edit", entity_type="pre_supervision_note", entity_id=note.id,
                    request=request, metadata={"version": note.revisions.count()})
        if first:
            notifications.preparation_submitted(appointment, request.user_account)
        return Response(scheduling.serialize_prep_note(note))


class PreparationTemplateView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Scheduling"], summary="The Supervision Preparation Form for this relationship's pathway", operation_id="prep_template", responses=JSON_RESPONSE)
    def get(self, request, pk):
        from apps.supervision.prep_templates import template_for

        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "relationship.view", rel, "relationship", rel.id):
            return denied
        key, template = template_for(rel.supervision_track)
        waiting = scheduling.standalone_prep(rel, request.user_account)
        linked = [
            {"appointment_id": str(note.appointment_id), "starts_at": note.appointment.starts_at.isoformat(),
             "ends_at": note.appointment.ends_at.isoformat(), "note": scheduling.serialize_prep_note(note)}
            for note in scheduling.linked_preps(rel, request.user_account)
        ]
        return Response({"key": key, **template, "can_prepare": has_permission(request.user_account, "prep.submit", relationship=rel),
                         "waiting": scheduling.serialize_prep_note(waiting), "linked": linked})

    @extend_schema(tags=["Scheduling"], summary="Start preparation before an appointment is scheduled (Supervisee)", request=PrepNoteSerializer, operation_id="prep_standalone_save", responses=JSON_RESPONSE)
    def put(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "prep.submit", rel, "pre_supervision_note", "standalone"):
            return denied
        serializer = PrepNoteSerializer(data=request.data)
        if not serializer.is_valid():
            return _invalid(serializer)
        try:
            note = scheduling.save_standalone_prep(rel, request.user_account, serializer.validated_data.get("answers") or {})
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="prep_note.save", entity_type="pre_supervision_note", entity_id=note.id, request=request,
                    metadata={"standalone": True, "version": note.revisions.count()})
        return Response(scheduling.serialize_prep_note(note))
