import json
import time
import redis
import openai
import hashlib
import tiktoken
import requests

import config
from log import log
logger = log(__name__).get_log_obj()

API_TOKEN = config.API_TOKEN

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 8192

try:
    OpenAIInvalidRequestError = openai.error.InvalidRequestError
except AttributeError:
    try:
        from openai import BadRequestError as OpenAIInvalidRequestError
    except ImportError:
        OpenAIInvalidRequestError = ()

class ResponseException(Exception):
    def __init__(self, info_dict):
        super().__init__(str(info_dict))
        self._info_dict = info_dict
        if 'message' in info_dict.keys():
            self._info_dict['_message'] = info_dict['message']

    @property
    def code(self):
        return self._info_dict.get("code") or self._info_dict.get("type")

    @property
    def message(self):
        return self._info_dict.get("message") or self._info_dict.get("_message") or str(self._info_dict)

    def __getattr__(self, item):
        try:
            return self._info_dict[item]
        except KeyError:
            raise AttributeError(f"'ResponseException' object has no attribute '{item}'")


def _is_anthropic_model(model_name: str) -> bool:
    name = (model_name or "").lower()
    return name.startswith("claude") or "claude" in name


def _resolve_channel(model_name, channel=None):
    if channel is not None:
        return channel
    if _is_anthropic_model(model_name):
        return "anthropic"
    if config.cloudflare_account_id and config.cloudflare_gateway:
        return "cloudflare"
    return "openai"


def _extract_anthropic_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "".join(parts)


def _normalize_anthropic_response(raw: dict, model_name: str) -> dict:
    text = _extract_anthropic_text(raw.get("content"))
    usage = raw.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or 0)
    return {
        "id": raw.get("id"),
        "model": raw.get("model") or model_name,
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "content": text,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _request_anthropic(model_name, user_prompt, system_prompt, max_tokens=DEFAULT_MAX_TOKENS):
    headers = {
        "x-api-key": API_TOKEN,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": model_name,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    response = requests.post(
        ANTHROPIC_API_URL, headers=headers, json=payload, timeout=60 * 10,
    )
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = {"error": {"type": "invalid_response", "message": response.text}}

    if response.status_code >= 400:
        err = body.get("error") or body
        code = err.get("type") or "api_error"
        if response.status_code == 429:
            code = "rate_limit_exceeded"
        raise ResponseException({
            "code": code,
            "message": err.get("message") or response.text,
            "status": response.status_code,
        })
    return _normalize_anthropic_response(body, model_name)


def request_api(model_name, user_prompt, system_prompt, channel=None,
                account_id=config.cloudflare_account_id, gateway=config.cloudflare_gateway):
    channel = _resolve_channel(model_name, channel)
    if channel == "anthropic":
        return _request_anthropic(model_name, user_prompt, system_prompt)
    if channel == "openai":
        openai.api_key = API_TOKEN
        return openai.ChatCompletion.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
    if channel == "cloudflare":
        if "gpt" in model_name:
            api_base_url = (
                f"https://gateway.ai.cloudflare.com/v1/{account_id}/{gateway}/openai/chat/completions"
            )
            headers = {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}
            data = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            response = requests.post(api_base_url, headers=headers, json=data, timeout=60 * 10)
            logger.info(response.text)
            return json.loads(response.text)
        raise ValueError(f"Cloudflare channel does not support model: {model_name}")
    raise ValueError(f"Invalid channel: {channel}")


def atomic_chat(system_prompt, user_prompt, max_times=5, model_name=None, use_cache=False):
    if use_cache:
        r = redis.StrictRedis(host='localhost', port=6379, db=0)
        all_prompt = model_name + system_prompt + user_prompt
        cache_key = hashlib.sha256(all_prompt.encode()).hexdigest()
        cached_response = r.get(cache_key)
        if cached_response:
            logger.debug(json.loads(cached_response))
            return json.loads(cached_response)
    try_times = 0
    while try_times < max_times:
        if try_times > 0:
            logger.debug(f"Try again... ({try_times+1}-th time)")
        try:
            response = request_api(model_name, user_prompt, system_prompt)
            if isinstance(response, dict) and "error" in response:
                raise ResponseException(response["error"])
            if "detail" in response.keys() and "This model's maximum context length is 32768 tokens. However, you requested " in response["detail"]:
                raise ResponseException({"code": "context_length_exceeded", "_message": response["detail"]})
            if use_cache:
                r.setex(cache_key, 60 * 60 * 24 * 365, json.dumps(response))
            break
        except Exception as e:
            logger.error(f"{try_times+1}-th time Error:{e}, Type(e):{type(e)}")
            if (isinstance(e, OpenAIInvalidRequestError) or isinstance(e, ResponseException)) and getattr(e, "code", None) == "context_length_exceeded":
                logger.error(e.code)
                logger.error(getattr(e, "_message", e.message))
                token_num = int(getattr(e, "_message", e.message).split("However, you requested ")[1].split(" tokens.")[0])
                logger.error(f"Token number: {token_num}")
                if token_num > 8192 and model_name == "gpt-4-1106-preview":
                    if token_num > 128000 - 4096:
                        enc = tiktoken.encoding_for_model(model_name)
                        length_system_prompt = len(enc.encode(system_prompt))
                        user_prompt = enc.decode(enc.encode(user_prompt)[:128000 - 4096 - length_system_prompt])
            elif (isinstance(e, OpenAIInvalidRequestError) or isinstance(e, ResponseException)) and getattr(e, "code", None) == "rate_limit_exceeded":
                msg = getattr(e, "message", str(e))
                if "Please try again in " in msg:
                    time_to_wait = msg.split("Please try again in ")[1].split("s.")[0]
                    if 'm' in time_to_wait:
                        time_to_wait = float(time_to_wait.split("m")[0]) * 60 + float(time_to_wait.split("m")[1].split("s")[0])
                    else:
                        time_to_wait = float(time_to_wait.split("s")[0])
                else:
                    time_to_wait = 30
                time.sleep(time_to_wait)
            else:
                time.sleep(30)
            try_times += 1
    return response


def chat(system_prompt, user_prompt, model_name, output_format=None, use_cache=False, remove_quote=False,
         db_manager=None, request_index=None):
    response = atomic_chat(system_prompt, user_prompt, model_name=model_name, use_cache=use_cache)

    if db_manager is not None and request_index is not None:
        db_manager.add_response(request_index, response)
    else:
        logger.debug(response)
    try:
        if "choices" in response.keys() and len(response['choices']) > 0:
            response_txt = response['choices'][0]['message']['content']
        elif "messages" in response.keys():
            response_txt = response['messages'][-1]['content']
        elif "content" in response.keys():
            response_txt = response["content"]
        else:
            raise KeyError("No recognized response content field")
    except Exception as e:
        logger.error(f"Error: {e}\nresponse:{response}")
        exit(1)
    if output_format is not None:
        check_system_prompt = "You are an Information Extraction Expert. You check if the output strictly follows the format."
        check_user_prompt = f"Please determine whether the following Content (between '```' and '```') meets the Format. If so, output just 'True'. Otherwise, extract the content part that strictly follows the Format from the Content as output.\nContent:```{response_txt}```\nFormat:{output_format}"
        check_response = atomic_chat(check_system_prompt, check_user_prompt, model_name=model_name, use_cache=use_cache)
        if not check_response['choices'][0]['message']['content'] == "True":
            response_txt = check_response['choices'][0]['message']['content']

    if remove_quote:
        response_txt = response_txt or ""
        if len(response_txt) >= 2 and (
            (response_txt[0] == "\'" and response_txt[-1] == "\'") or
            (response_txt[0] == "\"" and response_txt[-1] == "\"")
        ):
            response_txt = response_txt[1:-1]
        if not response_txt.strip():
            response_txt = "<NONE>"
    return response_txt
