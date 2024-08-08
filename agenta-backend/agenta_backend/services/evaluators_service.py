import re
import json
import asyncio
import logging
import traceback
from typing import Any, Dict, Union

import httpx
import numpy as np
from openai import OpenAI, AsyncOpenAI
from numpy._core._multiarray_umath import array
from autoevals.ragas import Faithfulness, ContextRelevancy

from agenta_backend.services.security import sandbox
from agenta_backend.models.shared_models import Error, Result
from agenta_backend.utils.event_loop_utils import ensure_event_loop
from agenta_backend.models.api.evaluation_model import (
    EvaluatorInputInterface,
    EvaluatorOutputInterface,
    EvaluatorMappingInputInterface,
    EvaluatorMappingOutputInterface,
)
from agenta_backend.utils.traces import process_distributed_trace_into_trace_tree


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def map(
    mapping_input: EvaluatorMappingInputInterface,
) -> EvaluatorMappingOutputInterface:
    """
    Maps the evaluator inputs based on the provided mapping and data tree.

    Returns:
        EvaluatorMappingOutputInterface: A dictionary containing the mapped evaluator inputs.
    """

    def get_nested_value(data: Dict[str, Any], key: str) -> Any:
        """
        Retrieves the nested value from a dictionary based on a dotted key path,
        where list indices can be included in square brackets.

        Args:
            data (Dict[str, Any]): The data dictionary to retrieve the value from.
            key (str): The key path to the desired value, with possible list indices.

        Returns:
            Any: The value found at the specified key path, or None if not found.

        Example:
            >>> data = {
            ...     'rag': {
            ...         'summarizer': [{'outputs': {'report': 'The answer is 42'}}]
            ...     }
            ... }
            >>> key = 'rag.summarizer[0].outputs.report'
            >>> get_nested_value(data, key)
            'The answer is 42'
        """

        pattern = re.compile(r"([^\[\].]+|\[\d+\])")
        keys = pattern.findall(key)

        for k in keys:
            if k.startswith("[") and k.endswith("]"):
                # Convert list index from '[index]' to integer
                k = int(k[1:-1])
                if isinstance(data, list):
                    data = data[k] if 0 <= k < len(data) else None
                else:
                    return None
            else:
                if isinstance(data, dict):
                    data = data.get(k, None)
                else:
                    return None
        return data

    mapping_outputs = {}
    for to_key, from_key in mapping_input.mapping.items():
        mapping_outputs[to_key] = get_nested_value(mapping_input.inputs, from_key)
    return {"outputs": mapping_outputs}


def get_correct_answer(
    data_point: Dict[str, Any], settings_values: Dict[str, Any]
) -> Any:
    """
    Helper function to retrieve the correct answer from the data point based on the settings values.

    Args:
        data_point (Dict[str, Any]): The data point containing the correct answer.
        settings_values (Dict[str, Any]): The settings values containing the key for the correct answer.

    Returns:
        Any: The correct answer from the data point.

    Raises:
        ValueError: If the correct answer key is not provided or not found in the data point.
    """
    correct_answer_key = settings_values.get("correct_answer_key")
    if correct_answer_key is None:
        raise ValueError("No correct answer keys provided.")
    if correct_answer_key not in data_point:
        raise ValueError(
            f"Correct answer column '{correct_answer_key}' not found in the test set."
        )
    return data_point[correct_answer_key]


def get_field_value_from_trace(tree: Dict[str, Any], key: str) -> Dict[str, Any]:
    """
    Retrieve the value of the key from the trace tree.

    Args:
        tree (Dict[str, Any]): The nested dictionary to retrieve the value from.
            i.e. inline trace
            e.g. tree["spans"]["rag"]["spans"]["retriever"]["internals"]["prompt"]
        key (str): The dot-separated key to access the value.
            e.g. rag.summarizer[0].outputs.report

    Returns:
        Dict[str, Any]: The retrieved value or None if the key does not exist or an error occurs.
    """

    def is_indexed(field):
        return "[" in field and "]" in field

    def parse(field):
        key = field
        idx = None

        if is_indexed(field):
            key = field.split("[")[0]
            idx = int(field.split("[")[1].split("]")[0])

        return key, idx

    SPECIAL_KEYS = [
        "inputs",
        "internals",
        "outputs",
    ]

    SPANS_SEPARATOR = "spans"

    spans_flag = True

    fields = key.split(".")

    try:
        for field in fields:
            # by default, expects something like 'retriever'
            key, idx = parse(field)

            # before 'SPECIAL_KEYS', spans are nested within a 'spans' key
            # e.g. trace["spans"]["rag"]["spans"]["retriever"]...
            if key in SPECIAL_KEYS:
                spans_flag = False

            # after 'SPECIAL_KEYS', it is a normal dict.
            # e.g. trace[...]["internals"]["prompt"]
            if spans_flag:
                tree = tree[SPANS_SEPARATOR]

            tree = tree[key]

            if idx is not None:
                tree = tree[idx]

        return tree

    # Suppress all Exception and leave Exception management to the caller.
    except Exception as e:
        logger.error(f"Error retrieving trace value from key: {traceback.format_exc()}")
        return None


def auto_exact_match(
    inputs: Dict[str, Any],  # pylint: disable=unused-argument
    output: str,
    data_point: Dict[str, Any],  # pylint: disable=unused-argument
    app_params: Dict[str, Any],  # pylint: disable=unused-argument
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    """
    Evaluator function to determine if the output exactly matches the correct answer.

    Args:
        inputs (Dict[str, Any]): The inputs for the evaluation.
        output (str): The output generated by the model.
        data_point (Dict[str, Any]): The data point containing the correct answer.
        app_params (Dict[str, Any]): The application parameters.
        settings_values (Dict[str, Any]): The settings values containing the key for the correct answer.
        lm_providers_keys (Dict[str, Any]): The language model provider keys.

    Returns:
        Result: A Result object containing the evaluation result.
    """
    try:
        correct_answer = get_correct_answer(data_point, settings_values)
        inputs = {"ground_truth": correct_answer, "prediction": output}
        response = exact_match(input=EvaluatorInputInterface(**{"inputs": inputs}))
        result = Result(type="bool", value=response["outputs"]["success"])
        return result
    except ValueError as e:
        return Result(
            type="error",
            value=None,
            error=Error(
                message=str(e),
            ),
        )
    except Exception as e:  # pylint: disable=broad-except
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during Auto Exact Match evaluation",
                stacktrace=str(traceback.format_exc()),
            ),
        )


def exact_match(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    prediction = input.inputs.get("prediction", "")
    ground_truth = input.inputs.get("ground_truth", "")
    success = True if prediction == ground_truth else False
    return {"outputs": {"success": success}}


def auto_regex_test(
    inputs: Dict[str, Any],  # pylint: disable=unused-argument
    output: str,
    data_point: Dict[str, Any],  # pylint: disable=unused-argument
    app_params: Dict[str, Any],  # pylint: disable=unused-argument
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    try:
        inputs = {"ground_truth": data_point, "prediction": output}
        response = regex_test(
            input=EvaluatorInputInterface(
                **{"inputs": inputs, "settings": settings_values}
            )
        )
        return Result(type="bool", value=response["outputs"]["success"])
    except Exception as e:  # pylint: disable=broad-except
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during Auto Regex evaluation",
                stacktrace=str(traceback.format_exc()),
            ),
        )


def regex_test(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    pattern = re.compile(input.settings["regex_pattern"], re.IGNORECASE)
    result = (
        bool(pattern.search(input.inputs["prediction"]))
        == input.settings["regex_should_match"]
    )
    return {"outputs": {"success": result}}


def auto_field_match_test(
    inputs: Dict[str, Any],  # pylint: disable=unused-argument
    output: str,
    data_point: Dict[str, Any],
    app_params: Dict[str, Any],  # pylint: disable=unused-argument
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    try:
        correct_answer = get_correct_answer(data_point, settings_values)
        inputs = {"ground_truth": correct_answer, "prediction": output}
        response = field_match_test(input=EvaluatorInputInterface(**{"inputs": inputs}))
        return Result(type="bool", value=response["outputs"]["success"])
    except ValueError as e:
        return Result(
            type="error",
            value=None,
            error=Error(
                message=str(e),
            ),
        )
    except Exception as e:  # pylint: disable=broad-except
        logging.debug("Field Match Test Failed because of Error: %s", str(e))
        return Result(type="bool", value=False)


def field_match_test(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    prediction_json = json.loads(input.inputs["prediction"])
    result = prediction_json == input.inputs["ground_truth"]
    return {"outputs": {"success": result}}


def auto_webhook_test(
    inputs: Dict[str, Any],
    output: str,
    data_point: Dict[str, Any],
    app_params: Dict[str, Any],  # pylint: disable=unused-argument
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    try:
        correct_answer = get_correct_answer(data_point, settings_values)
        inputs = {"prediction": output, "ground_truth": correct_answer}
        response = webhook_test(
            input=EvaluatorInputInterface(
                **{"inputs": inputs, "settings": settings_values}
            )
        )
        return Result(type="number", value=response["outputs"]["score"])
    except httpx.HTTPError as e:
        return Result(
            type="error",
            value=None,
            error=Error(
                message=f"[webhook evaluation] HTTP - {repr(e)}",
                stacktrace=traceback.format_exc(),
            ),
        )
    except json.JSONDecodeError as e:
        return Result(
            type="error",
            value=None,
            error=Error(
                message=f"[webhook evaluation] JSON - {repr(e)}",
                stacktrace=traceback.format_exc(),
            ),
        )
    except Exception as e:  # pylint: disable=broad-except
        return Result(
            type="error",
            value=None,
            error=Error(
                message=f"[webhook evaluation] Exception - {repr(e)} ",
                stacktrace=traceback.format_exc(),
            ),
        )


def webhook_test(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    with httpx.Client() as client:
        payload = {
            "correct_answer": input.inputs["ground_truth"],
            "output": input.inputs["prediction"],
            "inputs": input.inputs,
        }
        response = client.post(url=input.settings["webhook_url"], json=payload)
        response.raise_for_status()
        response_data = response.json()
        score = response_data.get("score", None)
        return {"outputs": {"score": score}}


def auto_custom_code_run(
    inputs: Dict[str, Any],
    output: str,
    data_point: Dict[str, Any],
    app_params: Dict[str, Any],
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    try:
        correct_answer = get_correct_answer(data_point, settings_values)
        inputs = {
            "app_config": app_params,
            "prediction": output,
            "ground_truth": correct_answer,
        }
        response = custom_code_run(
            input=EvaluatorInputInterface(
                **{"inputs": inputs, "settings": {"code": settings_values["code"]}}
            )
        )
        return Result(type="number", value=response["outputs"]["score"])
    except Exception as e:  # pylint: disable=broad-except
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during Auto Custom Code Evaluation",
                stacktrace=str(traceback.format_exc()),
            ),
        )


def custom_code_run(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    result = sandbox.execute_code_safely(
        app_params=input.inputs["app_config"],
        inputs=input.inputs,
        output=input.inputs["prediction"],
        correct_answer=input.inputs["ground_truth"],
        code=input.settings["code"],
        datapoint=input.inputs["ground_truth"],
    )
    return {"outputs": {"score": result}}


def auto_ai_critique(
    inputs: Dict[str, Any],
    output: str,
    data_point: Dict[str, Any],
    app_params: Dict[str, Any],
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],
) -> Result:
    """
    Evaluate a response using an AI critique based on provided inputs, output, correct answer, app parameters, and settings.

    Args:
        inputs (Dict[str, Any]): Input parameters for the LLM app variant.
        output (str): The output of the LLM app variant.
        correct_answer_key (str): The key name of the correct answer  in the datapoint.
        app_params (Dict[str, Any]): Application parameters.
        settings_values (Dict[str, Any]): Settings for the evaluation.
        lm_providers_keys (Dict[str, Any]): Keys for language model providers.

    Returns:
        Result: Evaluation result.
    """
    try:
        correct_answer = get_correct_answer(data_point, settings_values)
        inputs = {
            "prompt_user": app_params.get("prompt_user", ""),
            "prediction": output,
            "ground_truth": correct_answer,
        }
        response = ai_critique(
            input=EvaluatorInputInterface(
                **{"inputs": inputs, "credentials": lm_providers_keys}
            )
        )
        return Result(type="text", value=response["outputs"]["score"])
    except Exception as e:  # pylint: disable=broad-except
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during Auto AI Critique",
                stacktrace=str(traceback.format_exc()),
            ),
        )


def ai_critique(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    openai_api_key = input.credentials["OPENAI_API_KEY"]

    chain_run_args = {
        "llm_app_prompt_template": input.inputs.get("prompt_user", ""),
        "variant_output": input.inputs["prediction"],
        "correct_answer": input.inputs["ground_truth"],
    }

    for key, value in input.inputs.items():
        chain_run_args[key] = value

    prompt_template = input.settings["prompt_template"]
    messages = [
        {"role": "system", "content": prompt_template},
        {"role": "user", "content": str(chain_run_args)},
    ]

    client = OpenAI(api_key=openai_api_key)
    response = client.chat.completions.create(
        model="gpt-3.5-turbo", messages=messages, temperature=0.8
    )
    evaluation_output = response.choices[0].message.content.strip()
    return {"outputs": {"score": evaluation_output}}


def auto_starts_with(
    inputs: Dict[str, Any],  # pylint: disable=unused-argument
    output: str,
    data_point: Dict[str, Any],  # pylint: disable=unused-argument
    app_params: Dict[str, Any],  # pylint: disable=unused-argument
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    try:
        inputs = {"prediction": output}
        response = starts_with(
            input=EvaluatorInputInterface(
                **{"inputs": inputs, "settings": settings_values}
            )
        )
        return Result(type="text", value=response["outputs"]["success"])
    except Exception as e:  # pylint: disable=broad-except
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during Starts With evaluation",
                stacktrace=str(traceback.format_exc()),
            ),
        )


def starts_with(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    prefix = input.settings.get("prefix", "")
    case_sensitive = input.settings.get("case_sensitive", True)

    if not case_sensitive:
        output = str(input.inputs["prediction"]).lower()
        prefix = prefix.lower()

    result = output.startswith(prefix)
    return {"outputs": {"success": result}}


def auto_ends_with(
    inputs: Dict[str, Any],  # pylint: disable=unused-argument
    output: str,
    data_point: Dict[str, Any],  # pylint: disable=unused-argument
    app_params: Dict[str, Any],
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    try:
        inputs = {"prediction": output}
        response = ends_with(
            input=EvaluatorInputInterface(
                **{"inputs": inputs, "settings": settings_values}
            )
        )
        result = Result(type="bool", value=response["outputs"]["success"])
        return result
    except Exception as e:  # pylint: disable=broad-except
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during Ends With evaluation",
                stacktrace=str(traceback.format_exc()),
            ),
        )


def ends_with(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    suffix = input.settings.get("suffix", "")
    case_sensitive = input.settings.get("case_sensitive", True)

    if not case_sensitive:
        output = str(input.inputs["prediction"]).lower()
        suffix = suffix.lower()

    result = output.endswith(suffix)
    return {"outputs": {"success": result}}


def auto_contains(
    inputs: Dict[str, Any],  # pylint: disable=unused-argument
    output: str,
    data_point: Dict[str, Any],  # pylint: disable=unused-argument
    app_params: Dict[str, Any],  # pylint: disable=unused-argument
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    try:
        inputs = {"prediction": output}
        response = contains(
            input=EvaluatorInputInterface(
                **{"inputs": inputs, "settings": settings_values}
            )
        )
        result = Result(type="bool", value=response["outputs"["success"]])
        return result
    except Exception as e:  # pylint: disable=broad-except
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during Contains evaluation",
                stacktrace=str(traceback.format_exc()),
            ),
        )


def contains(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    substring = input.settings.get("substring", "")
    case_sensitive = input.settings.get("case_sensitive", True)

    if not case_sensitive:
        output = str(input.inputs["prediction"]).lower()
        substring = substring.lower()

    result = substring in output
    return {"outputs": {"success": result}}


def auto_contains_any(
    inputs: Dict[str, Any],  # pylint: disable=unused-argument
    output: str,
    data_point: Dict[str, Any],  # pylint: disable=unused-argument
    app_params: Dict[str, Any],  # pylint: disable=unused-argument
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    try:
        inputs = {"prediction": output}
        response = contains_any(
            input=EvaluatorInputInterface(
                **{"inputs": inputs, "settings": settings_values}
            )
        )
        result = Result(type="bool", value=response["outputs"]["success"])
        return result
    except Exception as e:  # pylint: disable=broad-except
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during Contains Any evaluation",
                stacktrace=str(traceback.format_exc()),
            ),
        )


def contains_any(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    substrings_str = input.settings.get("substrings", "")
    substrings = [substring.strip() for substring in substrings_str.split(",")]
    case_sensitive = input.settings.get("case_sensitive", True)

    if not case_sensitive:
        output = str(input.inputs["prediction"]).lower()
        substrings = [substring.lower() for substring in substrings]

    return {
        "outputs": {"success": any(substring in output for substring in substrings)}
    }


def auto_contains_all(
    inputs: Dict[str, Any],  # pylint: disable=unused-argument
    output: str,
    data_point: Dict[str, Any],  # pylint: disable=unused-argument
    app_params: Dict[str, Any],  # pylint: disable=unused-argument
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    try:
        response = contains_all(
            input=EvaluatorInputInterface(
                **{"inputs": {"prediction": output}, "settings": settings_values}
            )
        )
        result = Result(type="bool", value=response["outputs"]["success"])
        return result
    except Exception as e:  # pylint: disable=broad-except
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during Contains All evaluation",
                stacktrace=str(traceback.format_exc()),
            ),
        )


def contains_all(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    substrings_str = input.settings.get("substrings", "")
    substrings = [substring.strip() for substring in substrings_str.split(",")]
    case_sensitive = input.settings.get("case_sensitive", True)

    if not case_sensitive:
        output = str(input.inputs["prediction"]).lower()
        substrings = [substring.lower() for substring in substrings]

    result = all(substring in output for substring in substrings)
    return {"outputs": {"success": result}}


def auto_contains_json(
    inputs: Dict[str, Any],  # pylint: disable=unused-argument
    output: str,
    data_point: Dict[str, Any],  # pylint: disable=unused-argument
    app_params: Dict[str, Any],  # pylint: disable=unused-argument
    settings_values: Dict[str, Any],  # pylint: disable=unused-argument
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    try:
        response = contains_json(
            input=EvaluatorInputInterface(**{"inputs": {"prediction": output}})
        )
        return Result(type="bool", value=contains_json)
    except Exception as e:  # pylint: disable=broad-except
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during Contains JSON evaluation",
                stacktrace=str(traceback.format_exc()),
            ),
        )


def contains_json(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    start_index = str(input.inputs["prediction"]).index("{")
    end_index = str(input.inputs["prediction"]).rindex("}") + 1
    potential_json = str(input.inputs["prediction"])[start_index:end_index]

    try:
        json.loads(potential_json)
        contains_json = True
    except (ValueError, json.JSONDecodeError):
        contains_json = False

    return {"outputs": {"success": contains_json}}


def flatten_json(json_obj: Union[list, dict]) -> Dict[str, Any]:
    """
    This function takes a (nested) JSON object and flattens it into a single-level dictionary where each key represents the path to the value in the original JSON structure. This is done recursively, ensuring that the full hierarchical context is preserved in the keys.

    Args:
        json_obj (Union[list, dict]): The (nested) JSON object to flatten. It can be either a dictionary or a list.

    Returns:
        Dict[str, Any]: The flattened JSON object as a dictionary, with keys representing the paths to the values in the original structure.
    """

    output = {}

    def flatten(obj: Union[list, dict], path: str = "") -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                new_key = f"{path}.{key}" if path else key
                if isinstance(value, (dict, list)):
                    flatten(value, new_key)
                else:
                    output[new_key] = value

        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                new_key = f"{path}.{index}" if path else str(index)
                if isinstance(value, (dict, list)):
                    flatten(value, new_key)
                else:
                    output[new_key] = value

    flatten(json_obj)
    return output


def compare_jsons(
    ground_truth: Union[list, dict],
    app_output: Union[list, dict],
    settings_values: dict,
):
    """
    This function takes two JSON objects (ground truth and application output), flattens them using the `flatten_json` function, and then compares the fields.

    Args:
        ground_truth (list | dict): The ground truth
        app_output (list | dict): The application output
        settings_values: dict: The advanced configuration of the evaluator

    Returns:
        the average score between both JSON objects
    """

    def normalize_keys(d: Dict[str, Any], case_insensitive: bool) -> Dict[str, Any]:
        if not case_insensitive:
            return d
        return {k.lower(): v for k, v in d.items()}

    def diff(ground_truth: Any, app_output: Any, compare_schema_only: bool) -> float:
        gt_key, gt_value = next(iter(ground_truth.items()))
        ao_key, ao_value = next(iter(app_output.items()))

        if compare_schema_only:
            return (
                1.0 if (gt_key == ao_key and type(gt_value) == type(ao_value)) else 0.0
            )
        return 1.0 if (gt_key == ao_key and gt_value == ao_value) else 0.0

    flattened_ground_truth = flatten_json(ground_truth)
    flattened_app_output = flatten_json(app_output)

    keys = flattened_ground_truth.keys()
    if settings_values.get("predict_keys", False):
        keys = set(keys).union(flattened_app_output.keys())

    cumulated_score = 0.0
    no_of_keys = len(keys)

    compare_schema_only = settings_values.get("compare_schema_only", False)
    case_insensitive_keys = settings_values.get("case_insensitive_keys", False)
    flattened_ground_truth = normalize_keys(
        flattened_ground_truth, case_insensitive_keys
    )
    flattened_app_output = normalize_keys(flattened_app_output, case_insensitive_keys)

    for key in keys:
        ground_truth_value = flattened_ground_truth.get(key, None)
        llm_app_output_value = flattened_app_output.get(key, None)

        key_score = 0.0
        if ground_truth_value and llm_app_output_value:
            key_score = diff(
                {key: ground_truth_value},
                {key: llm_app_output_value},
                compare_schema_only,
            )

        cumulated_score += key_score

    average_score = cumulated_score / no_of_keys
    return average_score


def auto_json_diff(
    inputs: Dict[str, Any],  # pylint: disable=unused-argument
    output: Any,
    data_point: Dict[str, Any],  # pylint: disable=unused-argument
    app_params: Dict[str, Any],  # pylint: disable=unused-argument
    settings_values: Dict[str, Any],  # pylint: disable=unused-argument
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    try:
        correct_answer = get_correct_answer(data_point, settings_values)
        response = json_diff(
            input=EvaluatorInputInterface(
                **{
                    "inputs": {"prediction": output, "ground_truth": correct_answer},
                    "settings": settings_values,
                }
            )
        )
        return Result(type="number", value=response["outputs"]["score"])
    except (ValueError, json.JSONDecodeError, Exception):
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during JSON diff evaluation",
                stacktrace=traceback.format_exc(),
            ),
        )


def json_diff(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    average_score = compare_jsons(
        ground_truth=input.inputs["ground_truth"],
        app_output=json.loads(input.inputs["prediction"]),
        settings_values=input.settings,
    )
    return {"outputs": {"score": average_score}}


def rag_faithfulness(
    inputs: Dict[str, Any],  # pylint: disable=unused-argument
    output: Dict[str, Any],
    data_point: Dict[str, Any],  # pylint: disable=unused-argument
    app_params: Dict[str, Any],  # pylint: disable=unused-argument
    settings_values: Dict[str, Any],  # pylint: disable=unused-argument
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    try:
        if isinstance(output, str):
            logging.error("'output' is most likely not BaseResponse.")
            raise NotImplementedError(
                "Please update the SDK to the latest version, which supports RAG evaluators."
            )

        # Get required keys for rag evaluator
        question_key: Union[str, None] = settings_values.get("question_key", None)
        answer_key: Union[str, None] = settings_values.get("answer_key", None)
        contexts_key: Union[str, None] = settings_values.get("contexts_key", None)

        if None in [question_key, answer_key, contexts_key]:
            logging.error(
                f"Missing evaluator settings ? {['question', question_key is None, 'answer', answer_key is None, 'context', contexts_key is None]}"
            )
            raise ValueError(
                "Missing required configuration keys: 'question_key', 'answer_key', or 'contexts_key'. Please check your evaluator settings and try again."
            )

        # Turn distributed trace into trace tree
        trace = process_distributed_trace_into_trace_tree(output["trace"])

        # Get value of required keys for rag evaluator
        question_val: Any = get_field_value_from_trace(trace, question_key)
        answer_val: Any = get_field_value_from_trace(trace, answer_key)
        contexts_val: Any = get_field_value_from_trace(trace, contexts_key)

        if None in [question_val, answer_val, contexts_val]:
            logging.error(
                f"Missing trace field ? {['question', question_val is None, 'answer', answer_val is None, 'context', contexts_val is None]}"
            )

            message = ""
            if question_val is None:
                message += (
                    f"'question_key' is set to {question_key} which can't be found. "
                )
            if answer_val is None:
                message += f"'answer_key' is set to {answer_key} which can't be found. "
            if contexts_val is None:
                message += (
                    f"'contexts_key' is set to {contexts_key} which can't be found. "
                )
            message += "Please check your evaluator settings and try again."

            raise ValueError(message)

        openai_api_key = lm_providers_keys.get("OPENAI_API_KEY", None)

        if not openai_api_key:
            raise Exception(
                "No LLM keys OpenAI key found. Please configure your OpenAI keys and try again."
            )

        # Initialize RAG evaluator to calculate faithfulness score
        loop = ensure_event_loop()
        faithfulness = Faithfulness(api_key=openai_api_key)
        eval_score = loop.run_until_complete(
            faithfulness._run_eval_async(
                output=answer_val, input=question_val, context=contexts_val
            )
        )

        return Result(type="number", value=eval_score.score)

    except Exception:
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during RAG Faithfulness evaluation",
                stacktrace=str(traceback.format_exc()),
            ),
        )


def rag_context_relevancy(
    inputs: Dict[str, Any],  # pylint: disable=unused-argument
    output: Dict[str, Any],
    data_point: Dict[str, Any],  # pylint: disable=unused-argument
    app_params: Dict[str, Any],  # pylint: disable=unused-argument
    settings_values: Dict[str, Any],  # pylint: disable=unused-argument
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    try:
        if isinstance(output, str):
            logging.error("'output' is most likely not BaseResponse.")
            raise NotImplementedError(
                "Please update the SDK to the latest version, which supports RAG evaluators."
            )

        # Get required keys for rag evaluator
        question_key: Union[str, None] = settings_values.get("question_key", None)
        answer_key: Union[str, None] = settings_values.get("answer_key", None)
        contexts_key: Union[str, None] = settings_values.get("contexts_key", None)

        if None in [question_key, answer_key, contexts_key]:
            logging.error(
                f"Missing evaluator settings ? {['question', question_key is None, 'answer', answer_key is None, 'context', contexts_key is None]}"
            )
            raise ValueError(
                "Missing required configuration keys: 'question_key', 'answer_key', or 'contexts_key'. Please check your evaluator settings and try again."
            )

        # Turn distributed trace into trace tree
        trace = process_distributed_trace_into_trace_tree(output["trace"])

        # Get value of required keys for rag evaluator
        question_val: Any = get_field_value_from_trace(trace, question_key)
        answer_val: Any = get_field_value_from_trace(trace, answer_key)
        contexts_val: Any = get_field_value_from_trace(trace, contexts_key)

        if None in [question_val, answer_val, contexts_val]:
            logging.error(
                f"Missing trace field ? {['question', question_val is None, 'answer', answer_val is None, 'context', contexts_val is None]}"
            )

            message = ""
            if question_val is None:
                message += (
                    f"'question_key' is set to {question_key} which can't be found. "
                )
            if answer_val is None:
                message += f"'answer_key' is set to {answer_key} which can't be found. "
            if contexts_val is None:
                message += (
                    f"'contexts_key' is set to {contexts_key} which can't be found. "
                )
            message += "Please check your evaluator settings and try again."

            raise ValueError(message)

        openai_api_key = lm_providers_keys.get("OPENAI_API_KEY", None)

        if not openai_api_key:
            raise Exception(
                "No LLM keys OpenAI key found. Please configure your OpenAI keys and try again."
            )

        # Initialize RAG evaluator to calculate context relevancy score
        loop = ensure_event_loop()
        context_rel = ContextRelevancy(api_key=openai_api_key)
        eval_score = loop.run_until_complete(
            context_rel._run_eval_async(
                output=answer_val, input=question_val, context=contexts_val
            )
        )
        return Result(type="number", value=eval_score.score)

    except Exception:
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during RAG Context Relevancy evaluation",
                stacktrace=str(traceback.format_exc()),
            ),
        )


def levenshtein_distance(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    prediction = input.inputs["prediction"]
    ground_truth = input.inputs["ground_truth"]
    if len(prediction) < len(ground_truth):
        return levenshtein_distance(
            input=EvaluatorInputInterface(
                **{"inputs": {"prediction": prediction, "ground_truth": ground_truth}}
            )
        )  # pylint: disable=arguments-out-of-order

    if len(ground_truth) == 0:
        return len(s1)

    previous_row = range(len(ground_truth) + 1)
    for i, c1 in enumerate(prediction):
        current_row = [i + 1]
        for j, c2 in enumerate(ground_truth):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    result = previous_row[-1]
    if "threshold" in input.settings:
        threshold = input.settings["threshold"]
        is_within_threshold = distance <= threshold
        return {"outputs": {"success": is_within_threshold}}

    return {"outputs": {"score": distance}}


def auto_levenshtein_distance(
    inputs: Dict[str, Any],  # pylint: disable=unused-argument
    output: str,
    data_point: Dict[str, Any],
    app_params: Dict[str, Any],  # pylint: disable=unused-argument
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],  # pylint: disable=unused-argument
) -> Result:
    try:
        correct_answer = get_correct_answer(data_point, settings_values)
        response = levenshtein_distance(
            input=EvaluatorInputInterface(
                **{"inputs": {"prediction": output, "ground_truth": correct_answer}}
            )
        )
        return Result(type="number", value=response["outputs"].get("score", "success"))

    except ValueError as e:
        return Result(
            type="error",
            value=None,
            error=Error(
                message=str(e),
            ),
        )
    except Exception as e:  # pylint: disable=broad-except
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during Levenshtein threshold evaluation",
                stacktrace=str(traceback.format_exc()),
            ),
        )


def auto_similarity_match(
    inputs: Dict[str, Any],
    output: str,
    data_point: Dict[str, Any],
    app_params: Dict[str, Any],
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],
) -> Result:
    try:
        correct_answer = get_correct_answer(data_point, settings_values)
        response = similarity_match(
            input=EvaluatorInputInterface(
                **{
                    "inputs": {"prediction": output, "ground_truth": correct_answer},
                    "settings": settings_values,
                }
            )
        )
        result = Result(type="bool", value=response["outputs"]["success"])
        return result
    except ValueError as e:
        return Result(
            type="error",
            value=None,
            error=Error(
                message=str(e),
            ),
        )
    except Exception as e:  # pylint: disable=broad-except
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during Auto Similarity Match evaluation",
                stacktrace=str(traceback.format_exc()),
            ),
        )


def similarity_match(input: EvaluatorInputInterface) -> EvaluatorOutputInterface:
    set1 = set(input.inputs["prediction"].split())
    set2 = set(input.inputs["ground_truth"].split())
    intersect = set1.intersection(set2)
    union = set1.union(set2)

    similarity = len(intersect) / len(union)
    is_similar = True if similarity > input.settings["similarity_threshold"] else False
    return {"outputs": {"success": is_similar}}


async def semantic_similarity(
    input: EvaluatorInputInterface,
) -> EvaluatorOutputInterface:
    """Calculate the semantic similarity score of the LLM app using OpenAI's Embeddings API.

    Args:
        output (str): the output text
        correct_answer (str): the correct answer text

    Returns:
        float: the semantic similarity score
    """

    api_key = input.credentials["OPENAI_API_KEY"]
    openai = AsyncOpenAI(api_key=api_key)

    async def encode(text: str):
        response = await openai.embeddings.create(
            model="text-embedding-3-small", input=text
        )
        return np.array(response.data[0].embedding)

    def cosine_similarity(output_vector: array, correct_answer_vector: array) -> float:
        return np.dot(output_vector, correct_answer_vector)

    output_vector = await encode(input.inputs["prediction"])
    correct_answer_vector = await encode(input.inputs["ground_truth"])
    similarity_score = cosine_similarity(output_vector, correct_answer_vector)
    return {"outputs": {"score": similarity_score}}


def auto_semantic_similarity(
    inputs: Dict[str, Any],
    output: str,
    data_point: Dict[str, Any],
    app_params: Dict[str, Any],
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],
) -> Result:
    try:
        loop = ensure_event_loop()

        correct_answer = get_correct_answer(data_point, settings_values)
        inputs = {"prediction": output, "ground_truth": correct_answer}
        response = loop.run_until_complete(
            semantic_similarity(
                input=EvaluatorInputInterface(
                    **{
                        "inputs": inputs,
                        "credentials": lm_providers_keys,
                    }
                )
            )
        )
        return Result(type="number", value=response["outputs"]["score"])
    except Exception:
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error during Auto Semantic Similarity",
                stacktrace=str(traceback.format_exc()),
            ),
        )


EVALUATOR_FUNCTIONS = {
    "auto_exact_match": auto_exact_match,
    "auto_regex_test": auto_regex_test,
    "field_match_test": auto_field_match_test,
    "auto_webhook_test": auto_webhook_test,
    "auto_custom_code_run": auto_custom_code_run,
    "auto_ai_critique": auto_ai_critique,
    "auto_starts_with": auto_starts_with,
    "auto_ends_with": auto_ends_with,
    "auto_contains": auto_contains,
    "auto_contains_any": auto_contains_any,
    "auto_contains_all": auto_contains_all,
    "auto_contains_json": auto_contains_json,
    "auto_json_diff": auto_json_diff,
    "auto_semantic_similarity": auto_semantic_similarity,
    "auto_levenshtein_distance": auto_levenshtein_distance,
    "auto_similarity_match": auto_similarity_match,
    "rag_faithfulness": rag_faithfulness,
    "rag_context_relevancy": rag_context_relevancy,
}

NEW_EVALUATOR_FUNCTIONS = {
    "auto_exact_match": exact_match,
    "auto_regex_test": regex_test,
    "auto_field_match_test": field_match_test,
    "auto_webhook_test": webhook_test,
    "auto_custom_code_run": custom_code_run,
    "auto_ai_critique": ai_critique,
    "auto_starts_with": starts_with,
    "auto_ends_with": ends_with,
    "auto_contains": contains,
    "auto_contains_any": contains_any,
    "auto_contains_all": contains_all,
    "auto_contains_json": contains_json,
    "auto_json_diff": json_diff,
    "auto_levenshtein_distance": levenshtein_distance,
    "auto_similarity_match": similarity_match,
    "auto_semantic_similarity": semantic_similarity,
}


def evaluate(
    evaluator_key: str,
    inputs: Dict[str, Any],
    output: Union[str, Dict[str, Any]],
    data_point: Dict[str, Any],
    app_params: Dict[str, Any],
    settings_values: Dict[str, Any],
    lm_providers_keys: Dict[str, Any],
) -> Result:
    evaluation_function = EVALUATOR_FUNCTIONS.get(evaluator_key, None)
    if not evaluation_function:
        return Result(
            type="error",
            value=None,
            error=Error(
                message=f"Evaluation method '{evaluator_key}' not found.",
            ),
        )
    try:
        return evaluation_function(
            inputs,
            output,
            data_point,
            app_params,
            settings_values,
            lm_providers_keys,
        )
    except Exception as exc:
        return Result(
            type="error",
            value=None,
            error=Error(
                message="Error occurred while running {evaluator_key} evaluation. ",
                stacktrace=str(exc),
            ),
        )


def run(
    evaluator_key: str, evaluator_input: EvaluatorInputInterface
) -> EvaluatorOutputInterface:
    evaluator_function = NEW_EVALUATOR_FUNCTIONS.get(evaluator_key, None)
    if not evaluator_function:
        raise NotImplementedError(f"Evaluator {evaluator_key} not found")

    output = evaluator_function(evaluator_input)
    return output
