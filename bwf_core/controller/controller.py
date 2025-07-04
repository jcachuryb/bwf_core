import os
import json
from bwf_core import settings_bwf as settings
from bwf_core.models import ComponentInstance, ComponentStepStatusEnum
from importlib.machinery import SourceFileLoader
from django.core.cache import cache

BASE_PLUGIN_ROUTE = settings.BASE_PLUGIN_ROUTE
FLOW_NODES_ROUTE = settings.FLOW_NODES_ROUTE

IGNORE_DIRS = ["__pycache__"]


class BWFPluginController:
    _instance = None

    @staticmethod
    def get_instance():
        if BWFPluginController._instance is None:
            BWFPluginController._instance = BWFPluginController()
            if not BWFPluginController._instance.has_loaded:
                BWFPluginController._instance.load_plugins()
                BWFPluginController.has_loaded = True
        return BWFPluginController._instance

    has_loaded = False

    def __init__(self):
        self.plugins = {}

    def load_plugins(self):
        PLUGIN_ROUTES = [BASE_PLUGIN_ROUTE, FLOW_NODES_ROUTE]
        for route in PLUGIN_ROUTES:
            if not os.path.exists(route) or not os.path.isdir(route):
                continue
            for plugin in os.listdir(route):
                plugin_path = os.path.join(route, plugin)
                dir_name = plugin_path.split(os.sep)[-1]
                if os.path.isdir(plugin_path) and not dir_name in IGNORE_DIRS:
                    try:
                        new_plugin = self.__load_bwf_plugin(plugin_path)
                        if not new_plugin:
                            continue

                        new_plugin.get("settings")
                        self.plugins[new_plugin.get("id")] = new_plugin
                    except Exception as e:
                        print(f"Error loading plugin {plugin}: {e}")

    def __load_bwf_plugin(self, plugin_path):
        definition_json = os.path.join(plugin_path, "definition.json")
        if not os.path.exists(definition_json):
            raise Exception(
                f"Plugin {plugin_path} does not have a definition.json file"
            )

        plugin_definition = None
        with open(definition_json) as json_file:
            plugin_definition = json.load(json_file)

        # definition_module = SourceFileLoader("definition", definitions_path).load_module()
        # function_to_call = 'get_definition'

        # if not hasattr(definition_module, function_to_call):
        #     raise Exception(f"Plugin {plugin_path} does not have a '{function_to_call}' function in the definition.py file")

        plugin_ui_definition = {
            "icon_class": plugin_definition.get("icon_class"),
            "icon_image_src": plugin_definition.get("icon_image_src"),
        }

        plugin_workflow_type = plugin_definition.get("workflow_type", "*").lower()

        new_plugin = {
            "id": plugin_definition.get("id"),
            "name": plugin_definition.get("name"),
            "version": plugin_definition.get("version"),
            "description": plugin_definition.get("description"),
            "plugin_path": plugin_path,
            "workflow_type": plugin_workflow_type,
            "icon_class": plugin_definition.get("icon_class"),
            "icon_image_src": plugin_definition.get("icon_image_src"),
        }
        plugin_info = {
            "plugin_info": {
                "id": plugin_definition.get("id"),
                "version": plugin_definition.get("version"),
                "name": plugin_definition.get("name"),
                "description": plugin_definition.get("description"),
                "workflow_type": plugin_workflow_type,
            },
            "ui": plugin_ui_definition,
        }
        cache.set(
            f'plugin.info.{new_plugin.get("id")}',
            value=plugin_info,
            timeout=60 * 60 * 24,
        )
        # TODO: Validate it has all required fields

        return new_plugin

    @staticmethod
    def filter_plugins(is_long_lived=False, search=None):
        plugins_list = BWFPluginController.get_instance().__get_plugins_list()
        if is_long_lived:
            type_filter = ["long_lived", "*", "short_lived"]
        else:
            type_filter = ["short_lived", "*"]
        plugins_list = [
            plugin
            for plugin in plugins_list
            if plugin.get("workflow_type", "*").lower() in type_filter
        ]
        if search:
            plugins_list = [
                plugin
                for plugin in plugins_list
                if search.lower() in plugin.get("name", "").lower()
            ]
        return plugins_list

    @staticmethod
    def get_plugins_list():
        return BWFPluginController.get_instance().__get_plugins_list()

    @staticmethod
    def get_plugin_definition(plugin_id):
        return BWFPluginController.get_instance().__get_plugin_definition(plugin_id)

    def __get_plugins_list(self):
        return list(self.plugins.values())

    def __get_plugin_definition(self, plugin_id):
        plugin = self.plugins.get(plugin_id, None)
        if plugin:
            plugin_path = plugin.get("plugin_path")
            definition_json = os.path.join(plugin_path, "definition.json")
            plugin_definition = None
            with open(definition_json) as json_file:
                plugin_definition = json.load(json_file)

            base_inputs = plugin_definition.get("base_input")
            base_outputs = plugin_definition.get("base_output")
            workflow_inputs = plugin_definition.get("workflow_inputs")

            return {
                "id": plugin_definition.get("id"),
                "version": plugin_definition.get("version"),
                "name": plugin_definition.get("name"),
                "description": plugin_definition.get("description"),
                "workflow_type": plugin_definition.get("workflow_type", "*").lower(),
                "ui": {},
                "node_type": plugin_definition.get("node_type", "node"),
                "base_input": base_inputs,
                "base_output": base_outputs,
                "workflow_inputs": workflow_inputs,
            }
        return None

    def get_plugin_module(self, plugin_id, version_number="1"):
        plugin = self.plugins.get(plugin_id, None)
        if plugin:
            plugin_path = plugin.get("plugin_path")
            component_path = os.path.join(plugin_path, "component.py")
            component_module = SourceFileLoader(
                "component", component_path
            ).load_module()
            return component_module
        return None

    def get_plugin_definition_info(self, plugin_id):
        plugin = self.plugins.get(plugin_id, None)
        if plugin:
            ui_definition = cache.get(f"plugin.info.{plugin_id}")
            if ui_definition:
                return ui_definition

            plugin_path = plugin.get("plugin_path")
            definition_json = os.path.join(plugin_path, "definition.json")
            plugin_definition = None
            with open(definition_json) as json_file:
                plugin_definition = json.load(json_file)

            ui_definition = {
                "icon_class": plugin_definition.get("icon_class"),
                "icon_image_src": plugin_definition.get("icon_image_src"),
            }

            plugin_info = {
                "plugin_info": {
                    "id": plugin_definition.get("id"),
                    "version": plugin_definition.get("version"),
                    "name": plugin_definition.get("name"),
                    "description": plugin_definition.get("description"),
                },
                "ui": ui_definition,
            }

            cache.set(
                f"plugin.info.{plugin_id}", value=plugin_info, timeout=60 * 60 * 24
            )
            return plugin_info


class WorkflowController:
    pass
