var workflow_inputs = {
  workflow_id: null,
  version_id: null,
  is_edition: false,
  add_input_btn: null,
  containerId: null,
  container: null,

  selectedInput: null,
  progressBar: null,
  progressContainer: null,
  inputsController: null,
  variablesController: null,
  componentsController: null,
  var: {
    base_url: "/bwf/api/workflow-inputs/",
    inputs: [],
  },

  init: function (options, containerId) {
    const _ = workflow_inputs;
    const { workflow_id, version_id, is_edition } = options;
    if (!workflow_id || !version_id || !containerId) {
      console.error("workflow_id and containerId are required");
      console.error("workflow_id is required");
      return;
    }
    _.is_edition = is_edition;
    _.workflow_id = workflow_id;
    _.version_id = version_id;
    _.containerId = containerId;
    _.container = $(`#${containerId} #inputs`);
    _.add_input_btn = $(`#${containerId} button.add-input`);
    // Add + Buttton
    _.api.refreshInputs();

    if (_.is_edition) {
      _.add_input_btn.on("click", function () {
        const _ = workflow_inputs;
        _.selectedInput = null;
        $("#inputs-modal").modal("show");
      });
    } else {
      _.add_input_btn.remove();
    }
  },
  renderInputs: function () {
    const _ = workflow_inputs;
    _.container.empty();
    const inputs = _.var.inputs;
    for (let i = 0; i < inputs.length; i++) {
      const input = inputs[i];
      _.appendInput(input);
    }
  },
  appendInput: function (input) {
    const _ = workflow_inputs;
    const elementId = `input_${input.id}`;
    const inputMarkup = `
          <label id="${elementId}" class="list-group-item d-flex gap-1">
            <div class="d-flex gap-2 justify-content-between w-100">
            <div style="line-height: 15px">
              <span class='input-label'>
                ${input.label}
              </span></br>
              <span style="font-size: 10px; color: #6c757d" class="input-key">
                ${input.key}
              </span>
              <code style="font-size: 10px" class='input-type'>${input.data_type}</code>
              
            </div>
            <div class="form-check form-switch">
              <button class="btn btn-ghost add-input"data-input-id="${input.id}">
                <i class="bi bi-gear"></i>
              </button>
              <button class="btn btn-ghost remove-input" data-input-id="${input.id}">
                <i class="bi bi-trash text-danger"></i>
              </button>
            </div>
            </div>
          </label>
        `;
    _.container.append(inputMarkup);

    if (!_.is_edition) {
      $(`#${elementId} button.add-input`).remove();
      $(`#${elementId} button.remove-input`).remove();
    }
    if (input.parent_component) {
      $(`#${elementId} button.remove-input`).remove();
    }


    $(`#${elementId}`)
      .find(`button.add-input`)
      ?.on("click", function () {
        const _ = workflow_inputs;
        _.selectedInput = _.var.inputs.find((i) => i.id == input.id);
        $("#inputs-modal").modal("show");
      });
    $(`#${elementId}`)
      .find(`button.remove-input`)
      ?.on("click", function () {
        $(`#${elementId}`).find(`button.remove-input`).prop("disabled", true);
        const _ = workflow_inputs;
        _.selectedInput = _.var.inputs.find((i) => i.id == input.id);
        workflow_inputs.api.deleteInput(
          input,
          function (data) {
            $(`#${elementId} button.remove-input`).prop("disabled", false);
            $(`#${elementId}`).remove();
            _.var.inputs = _.var.inputs.filter((i) => i.id != input.id);
            const errors = data.errors || [];
            component_utils.markupErrors(errors);
          },
          function (error) {
            $(`#${elementId} button.remove-input`).prop("disabled", false);
            console.error(error);
            alert("Error deleting input. Please try again later.");
          }
        );
      });
  },
  updateInput: function (input) {
    const element = $(`#input_${input.id}`);

    element.find(`.input-label`).text(input.label);
    element.find(`.input-key`).text(input.key);
    element.find(`.input-type`).text(input.data_type);
  },
  api: {
    addInput: function (input, success_callback, error_callback) {
      const _ = workflow_inputs;
      $.ajax({
        url: _.var.base_url,
        type: "POST",
        headers: { "X-CSRFToken": $("#csrf_token").val() },
        contentType: "application/json",
        data: JSON.stringify({
          ...input,
          workflow_id: _.workflow_id,
          version_id: _.version_id,
        }),
        success: success_callback,
        error: error_callback,
      });
    },
    updateInput: function (input, success_callback, error_callback) {
      const _ = workflow_inputs;
      $.ajax({
        url: _.var.base_url + input.id + "/",
        type: "PUT",
        headers: { "X-CSRFToken": $("#csrf_token").val() },
        contentType: "application/json",
        data: JSON.stringify({
          ...input,
          workflow_id: _.workflow_id,
          version_id: _.version_id,
        }),
        success: success_callback,
        error: error_callback,
      });
    },
    deleteInput: function (input, success_callback, error_callback) {
      const _ = workflow_inputs;
      const _params = {
        workflow_id: _.workflow_id,
        version_id: _.version_id,
      };
      const queryParams = utils.make_query_params(_params);
      $.ajax({
        url: _.var.base_url + input.id + "/?" + queryParams,
        type: "DELETE",
        headers: { "X-CSRFToken": $("#csrf_token").val() },
        contentType: "application/json",
        success: success_callback,
        error: error_callback,
      });
    },
    refreshInputs: function () {
      const _ = workflow_inputs;
      const _params = {
        workflow_id: _.workflow_id,
        version_id: _.version_id,
      };
      const queryParams = utils.make_query_params(_params);
      $.ajax({
        url: _.var.base_url + "?" + queryParams,
        type: "GET",
        success: function (data) {
          _.var.inputs = data;
          _.renderInputs();
        },
        error: function (error) {
          console.error(error);
        },
      });
    },
  },
};
