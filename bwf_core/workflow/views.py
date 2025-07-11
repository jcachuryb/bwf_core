import uuid
import mimetypes
import json
import logging
from rest_framework import status

from rest_framework.decorators import action, permission_classes, api_view
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet, ViewSet
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.core.paginator import Paginator
from django.db.models import Max, Prefetch, Q
from django.db import transaction
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, get_object_or_404
from .utils import generate_workflow_definition, set_workflow_active_version
from bwf_core.models import Workflow, WorkflowVersion
from bwf_core.workflow.serializers import workflow_serializers
from bwf_core.workflow.tasks import (
    validate_workflow_required_fields,
    validate_workflow_routing_configuration,
    add_workflow_input_field,
)
from bwf_core.tasks import start_workflow
from bwf_core.components.tasks import (
    extract_workflow_mapping,
    apply_workflow_entry_removal,
)
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

# Create your views here.


class WorkflowViewset(ModelViewSet):
    queryset = Workflow.objects.all()
    serializer_class = workflow_serializers.WorkflowSerializer

    def get_serializer_class(self):
        if self.action == "list":
            return workflow_serializers.ListWorkflowSerializer
        elif self.action == "create" or self.action == "update":
            return workflow_serializers.CreateWorkflowSerializer
        return super().get_serializer_class()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        instance = Workflow.objects.create(**serializer.validated_data)
        workflow_definition = generate_workflow_definition(
            instance.name, instance.description
        )
        # save definition as json
        json_data = json.dumps(workflow_definition)
        temp_name = str(uuid.uuid4())
        file_name = f"workflow_{temp_name}.json"
        instance.workflow_file.save(file_name, ContentFile(json_data))

        edition_instance = WorkflowVersion.objects.create(
            workflow=instance,
            version_number=instance.version_number,
            version_name="Untitled",
        )
        edition_instance.workflow_file.save(file_name, ContentFile(json_data))

        return Response(
            workflow_serializers.WorkflowVersionSerializer(edition_instance).data
        )

    def update(self, request, *args, **kwargs):

        # update version Name
        workflow_id = kwargs.get("pk")
        workflow = get_object_or_404(
            Workflow, id=workflow_id
        )
        serializer = self.get_serializer(workflow, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        workflow.name = serializer.validated_data.get("name", workflow.name)
        workflow.description = serializer.validated_data.get("description", workflow.description)
        workflow.save()

        return Response(serializer.data)

    def list(self, request, *args, **kwargs):

        page_param = request.GET.get("page", "1")
        page_size_param = request.GET.get("page_size", "10")
        sort_column = request.GET.get("sort_column", "updated_at")
        sort_order = request.GET.get("sort_order", "desc")

        search = request.GET.get("search", "")
        order_by_val = f"{'-' if sort_order == 'desc' else ''}{sort_column}"
        wf_versions_queryset = (
            WorkflowVersion.objects.filter(is_disabled=False)
            .filter(Q(is_edition=True) | Q(is_active=True))
            .only(
                "workflow_id",
                "version_number",
                "version_name",
                "created_at",
                "updated_at",
            )
            .order_by("is_active", "-updated_at")
        )
        workflows = (
            Workflow.objects.all()
            .prefetch_related(Prefetch("versions", queryset=wf_versions_queryset))
            .order_by(order_by_val)
        )

        if search != "":
            workflows = workflows.filter(name__icontains=search)
        paginator = Paginator(workflows, page_size_param)
        page = paginator.page(page_param)
        return JsonResponse(
            {
                "count": paginator.count,
                "hasPrevious": page.has_previous(),
                "hasNext": page.has_next(),
                "results": self.get_serializer(page.object_list, many=True).data,
            }
        )

    @action(detail=False, methods=["post"], permission_classes=[AllowAny])
    def trigger_workflow(self, request):
        workflow_id = request.data.get("workflow_id")
        payload = request.data.get("payload", {})
        try:
            workflow = Workflow.objects.get(id=workflow_id)
            instance = start_workflow(workflow_id, payload)
            return JsonResponse(
                {"success": True, "message": "Workflow started", "data": instance}
            )
        except Exception as e:
            return JsonResponse({"success": False, "message": str(e)})


class WorkflowVersionViewset(ModelViewSet):
    queryset = WorkflowVersion.objects.all()
    serializer_class = workflow_serializers.WorkflowSerializer

    def get_serializer_class(self):
        if self.action == "create" or self.action == "update":
            return workflow_serializers.CreateWorkflowVersionSerializer
        return super().get_serializer_class()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workflow_id = serializer.validated_data.get("workflow_id", None)
        version_id = serializer.validated_data.get("version_id", None)
        instance = None
        name = ""
        version = None
        parent_version = None
        if version_id:
            parent_version = get_object_or_404(
                WorkflowVersion, id=version_id, workflow__id=workflow_id
            )
            instance = parent_version.workflow
            name = f"Edition of version {parent_version.version_number}"
        else:
            instance = get_object_or_404(Workflow, id=workflow_id)
            name = instance.name

        workflow_definition = instance.get_json_definition()
        version_definition = generate_workflow_definition(name, instance.description)
        version_definition["edition"] = True
        version_definition["workflow"] = workflow_definition["workflow"]
        version_definition["mapping"] = workflow_definition["mapping"]
        version_definition["inputs"] = workflow_definition["inputs"]
        version_definition["variables"] = workflow_definition["variables"]

        json_data = json.dumps(version_definition)
        temp_name = str(uuid.uuid4())
        file_name = f"workflow_edition_{temp_name}.json"
        version_number = instance.versions.aggregate(
            version_number=Max("version_number")
        ).get("version_number", 0)
        version_number = version_number + 1 if version_number else 1
        try:
            with transaction.atomic():
                instance_edition = WorkflowVersion.objects.create(
                    workflow=instance,
                    version_number=version_number,
                    version_name=serializer.validated_data.get("name", "Untitled"),
                    parent_version=parent_version,
                )

                instance_edition.workflow_file.save(file_name, ContentFile(json_data))
                return Response(
                    workflow_serializers.WorkflowVersionSerializer(
                        instance_edition
                    ).data
                )
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {"error": "Error creating workflow edition"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    def update(self, request, *args, **kwargs):

        # update version Name
        version_id = kwargs.get("pk")
        workflow_id = request.query_params.get("workflow_id", None)
        version = get_object_or_404(
            WorkflowVersion, id=version_id, workflow__id=workflow_id
        )
        serializer = self.get_serializer(version, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        version.name = serializer.validated_data.get("name", version.name)
        version.save()

        return Response(serializer.data)

    @action(detail=True, methods=["POST"])
    def mark_workflow_active_version(self, request, *args, **kwargs):
        version_id = kwargs.get("pk")
        workflow_id = request.query_params.get("workflow_id", None)
        version = get_object_or_404(
            WorkflowVersion, id=version_id, workflow__id=workflow_id
        )
        try:
            if version.is_disabled:
                return JsonResponse(
                    {
                        "success": False,
                        "message": "Cannot set an inactive version as active.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        
            if version.is_active:
                return JsonResponse(
                    {
                        "success": True,
                        "message": "This version is already active.",
                    },
                    status=status.HTTP_200_OK,
                )
            
            errors = validate_workflow_required_fields(
                version.get_json_definition().get("workflow", {})
            )
            errors += validate_workflow_routing_configuration(
                version.get_json_definition()
            )
            if len(errors):
                return JsonResponse(
                    {
                        "success": False,
                        "message": "The Workflow has missing values.",
                        "errors": errors,
                    }
                )
            set_workflow_active_version(version)
        except Exception as e:
            logger.error(f"Error setting active version: {str(e)}")
            return JsonResponse(
                {"success": False, "message": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return JsonResponse({"success": True, "message": "Active version changed"})

    @action(detail=True, methods=["POST"])
    def discard_workflow_version(self, request, *args, **kwargs):
        version_id = kwargs.get("pk")
        workflow_id = request.query_params.get("workflow_id", None)
        version_number = request.query_params.get("version_number", None)
        version = get_object_or_404(
            WorkflowVersion, id=version_id, workflow__id=workflow_id, version_number=version_number
        )

        if version.is_active:
            return JsonResponse(
                {
                    "success": False,
                    "message": "Cannot discard an active version. Please set another version as active first.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        version.deactivate_version()
        return JsonResponse({"success": True, "message": "Version deactivated"})

    @action(detail=True, methods=["POST"])
    def extract_workflow_mapping(self, request, *args, **kwargs):
        version_id = kwargs.get("pk")
        workflow_id = request.query_params.get("workflow_id", None)
        version = get_object_or_404(
            WorkflowVersion, id=version_id, workflow__id=workflow_id
        )
        try:
            workflow_definition = version.get_json_definition()
            extract_workflow_mapping(
                workflow_definition.get("workflow", {}),
                workflow_definition.get("mapping", {}),
            )
            version.set_json_definition(workflow_definition)
        except Exception as e:
            logger.error(f"Error setting active version: {str(e)}")
            return JsonResponse(
                {"success": False, "message": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return JsonResponse({"success": True, "message": "Active version changed"})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_workflow_file(request, id, version):
    workflow = get_object_or_404(Workflow, id=id)
    file = workflow.workflow_file
    if version:
        workflow = get_object_or_404(Workflow, id=id)
        version = get_object_or_404(WorkflowVersion, workflow=workflow, id=version)
        file = version.workflow_file

    if file is None:
        return HttpResponse("File doesn't exist", status=status.HTTP_404_NOT_FOUND)

    file_data = None
    try:
        with open(file.path, "rb") as f:
            file_data = f.read()
            f.close()
    except Exception as e:
        return render(request, "govapp/404.html", context={})
    return HttpResponse(file_data, content_type=mimetypes.types_map[".json"])


class WorkflowInputsViewset(ViewSet):

    def get_serializer(self, *args, **kwargs):
        if self.action == "create" or self.action == "update":
            return workflow_serializers.CreateWorkflowInputSerializer(*args, **kwargs)
        return super().get_serializer(*args, **kwargs)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workflow_id = serializer.validated_data.get("workflow_id", None)
        version_id = serializer.validated_data.get("version_id", None)
        workflow = get_object_or_404(
            WorkflowVersion, id=version_id, workflow__id=workflow_id
        )

        if not workflow.is_editable:
            return Response(
                {"error": "Workflow version cannot be edited"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        workflow_definition = workflow.get_json_definition()
        new_input = add_workflow_input_field(
            workflow_definition,
            key=serializer.validated_data.get("key"),
            input_field={
                "label": serializer.validated_data.get("label"),
                "description": serializer.validated_data.get("description", ""),
                "data_type": serializer.validated_data.get("data_type"),
                "default_value": serializer.validated_data.get("default_value", {}),
                "required": serializer.validated_data.get("required", False),
            },
        )
        workflow.set_json_definition(workflow_definition)

        return Response(workflow_serializers.WorkflowInputSerializer(new_input).data)

    def retrieve(self, request, *args, **kwargs):
        workflow_id = request.query_params.get("workflow_id", None)
        version_id = request.query_params.get("version_id", None)
        workflow = get_object_or_404(
            WorkflowVersion, id=version_id, workflow__id=workflow_id
        )
        workflow_definition = workflow.get_json_definition()
        workflow_inputs = workflow_definition.get("inputs", {})
        instance = workflow_inputs.get(kwargs.get("pk"), None)
        return Response(workflow_serializers.WorkflowInputSerializer(instance).data)

    def list(self, request, *args, **kwargs):
        workflow_id = request.query_params.get("workflow_id", None)
        version_id = request.query_params.get("version_id", None)
        try:
            workflow = get_object_or_404(
                WorkflowVersion, id=version_id, workflow__id=workflow_id
            )
            workflow_definition = workflow.get_json_definition()
            workflow_inputs = workflow_definition.get("inputs", {})
            return Response(
                workflow_serializers.WorkflowInputSerializer(
                    list(workflow_inputs.values()), many=True
                ).data
            )
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def update(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workflow_id = serializer.validated_data.get("workflow_id", None)
        version_id = serializer.validated_data.get("version_id", None)

        workflow = get_object_or_404(
            WorkflowVersion, id=version_id, workflow__id=workflow_id
        )

        if not workflow.is_editable:
            return Response(
                {"error": "Workflow version cannot be edited"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        workflow_definition = workflow.get_json_definition()
        workflow_inputs = workflow_definition.get("inputs", {})

        instance = workflow_inputs.get(kwargs.get("pk"), None)
        if not instance:
            raise Exception("Input not found")

        key = serializer.validated_data.get("key")
        has_key_changed = key != instance["key"]
        if has_key_changed:
            if workflow_inputs.get(key, None):
                key = f"{key}_{str(uuid.uuid4())[0:8]}"
                instance["key"] = key

        instance["label"] = serializer.validated_data.get("label")
        instance["description"] = serializer.validated_data.get("description", "")
        instance["data_type"] = serializer.validated_data.get("data_type")
        instance["default_value"] = serializer.validated_data.get("default_value", {})
        instance["required"] = serializer.validated_data.get("required", False)
        workflow_inputs[instance["id"]] = instance

        workflow.set_json_definition(workflow_definition)
        return Response(workflow_serializers.WorkflowInputSerializer(instance).data)

    def destroy(self, request, *args, **kwargs):
        workflow_id = request.query_params.get("workflow_id", None)
        version_id = request.query_params.get("version_id", None)

        workflow = get_object_or_404(
            WorkflowVersion, id=version_id, workflow__id=workflow_id
        )

        if not workflow.is_editable:
            return Response(
                {"error": "Workflow version cannot be edited"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        workflow_definition = workflow.get_json_definition()
        workflow_inputs = workflow_definition.get("inputs", {})
        instance_id = kwargs.get("pk")
        instance = workflow_inputs.get(instance_id, None)
        if not instance:
            raise Exception("Input not found")

        workflow_inputs.pop(kwargs.get("pk"), None)

        is_input = True
        key = instance.get("key", None)
        id = instance.get("id", None)
        errors = apply_workflow_entry_removal(
            workflow_definition.get("workflow", {}), is_input, key, id
        )
        workflow.set_json_definition(workflow_definition)
        return JsonResponse(
            {"success": True, "message": "Input removed", "errors": errors}
        )


class WorkflowVariablesViewset(ViewSet):
    serializer_class = workflow_serializers.VariableValueSerializer

    def get_serializer(self, *args, **kwargs):
        if self.action == "create" or self.action == "update":
            return workflow_serializers.CreateVariableValueSerializer(*args, **kwargs)
        return super().get_serializer(*args, **kwargs)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workflow_id = serializer.validated_data.get("workflow_id", None)
        version_id = serializer.validated_data.get("version_id", None)

        workflow = get_object_or_404(
            WorkflowVersion, id=version_id, workflow__id=workflow_id
        )

        if not workflow.is_editable:
            return Response(
                {"error": "Workflow version cannot be edited"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        workflow_definition = workflow.get_json_definition()
        workflow_variables = workflow_definition.get("variables", {})
        key = serializer.validated_data.get("key")
        var_id = str(uuid.uuid4())
        if key in workflow_variables:
            key = f"{key}_{str(uuid.uuid4())[0:8]}"
        new_variable = {
            "id": var_id,
            "name": serializer.validated_data.get("name"),
            "key": key,
            "value": serializer.validated_data.get("value", ""),
            "data_type": serializer.validated_data.get("data_type"),
            "context": serializer.validated_data.get("context", "variables"),
        }
        workflow_variables[var_id] = new_variable
        workflow_definition["variables"] = workflow_variables
        workflow.set_json_definition(workflow_definition)
        return Response(workflow_serializers.VariableValueSerializer(new_variable).data)

    def list(self, request, *args, **kwargs):
        workflow_id = request.query_params.get("workflow_id", None)
        version_id = request.query_params.get("version_id", None)
        try:
            workflow = get_object_or_404(
                WorkflowVersion, id=version_id, workflow__id=workflow_id
            )
            workflow_definition = workflow.get_json_definition()
            workflow_variables = workflow_definition.get("variables", {})
            return Response(
                workflow_serializers.VariableValueSerializer(
                    list(workflow_variables.values()), many=True
                ).data
            )
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def update(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workflow_id = serializer.validated_data.get("workflow_id", None)
        version_id = serializer.validated_data.get("version_id", None)

        workflow = get_object_or_404(
            WorkflowVersion, id=version_id, workflow__id=workflow_id
        )

        if not workflow.is_editable:
            return Response(
                {"error": "Workflow version cannot be edited"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        workflow_definition = workflow.get_json_definition()
        workflow_variables = workflow_definition.get("variables", {})
        instance = workflow_variables.get(kwargs.get("pk"), None)
        if not instance:
            raise Exception("Variable not found")

        key = serializer.validated_data.get("key")
        has_key_changed = key != instance["key"]
        if has_key_changed:
            if workflow_variables.get(key, None):
                key = f"{key}_{str(uuid.uuid4())[0:8]}"
                instance["key"] = key

        instance["name"] = serializer.validated_data.get("name")
        instance["data_type"] = serializer.validated_data.get("data_type")
        instance["value"] = serializer.validated_data.get("value", "")
        instance["context"] = serializer.validated_data.get("context", "variables")
        workflow_variables[instance["id"]] = instance

        workflow.set_json_definition(workflow_definition)
        return Response(workflow_serializers.VariableValueSerializer(instance).data)

    def destroy(self, request, *args, **kwargs):
        workflow_id = request.query_params.get("workflow_id", None)
        version_id = request.query_params.get("version_id", None)

        workflow = get_object_or_404(
            WorkflowVersion, id=version_id, workflow__id=workflow_id
        )

        if not workflow.is_editable:
            return Response(
                {"error": "Workflow version cannot be edited"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        workflow_definition = workflow.get_json_definition()
        workflow_variables = workflow_definition.get("variables", {})
        variable = workflow_variables.get(kwargs.get("pk"), None)
        if not variable:
            raise Exception("Variable not found")

        instance = workflow_variables.pop(kwargs.get("pk"), None)

        is_input = False
        key = instance.get("key", None)
        id = instance.get("id", None)
        errors = apply_workflow_entry_removal(
            workflow_definition.get("workflow", {}), is_input, key, id
        )
        workflow.set_json_definition(workflow_definition)
        return JsonResponse(
            {"success": True, "message": "Variable removed", "errors": errors}
        )
