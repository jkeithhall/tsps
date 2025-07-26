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
                    
class ResponseException(Exception):
    def __init__(self, info_dict):
        super().__init__(str(info_dict))
        self._info_dict = info_dict
        if 'message' in info_dict.keys():
            self._info_dict['_message'] = info_dict['message']

    def __getattr__(self, item):
        try:
            return self._info_dict[item]
        except KeyError:
            raise AttributeError(f"'ResponseException' object has no attribute '{item}'")


def request_api(model_name, user_prompt, system_prompt, channel = "cloudflare", 
                account_id = config.cloudflare_account_id, gateway=config.cloudflare_gateway):
    if channel == "openai":
        openai.api_key = API_TOKEN
        return openai.ChatCompletion.create(model=model_name,
                                            messages=[{"role": "system", "content": system_prompt},
                                                      {"role": "user", "content": user_prompt},])
    elif channel  == "cloudflare":
        if "gpt" in model_name:
            API_BASE_URL = f"https://gateway.ai.cloudflare.com/v1/{account_id}/{gateway}/openai/chat/completions"
            headers = {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}
            data = {"model": model_name,
                    "messages": [{"role": "system", "content": system_prompt},
                                 {"role": "user", "content": user_prompt}]}
        response = requests.post(API_BASE_URL, headers=headers, json=data, timeout=60*10)
        logger.info(response.text)
        return json.loads(response.text)
    else:
        raise ValueError(f"Invalid channel: {channel}")


def atomic_chat(system_prompt, user_prompt, max_times=5, model_name=None, use_cache=False):
    if use_cache:
        r = redis.StrictRedis(host='localhost', port=6379, db=0)
        all_prompt = model_name+system_prompt+user_prompt
        cache_key = hashlib.sha256(all_prompt.encode()).hexdigest()
        cached_response = r.get(cache_key)#.decode("utf-8")
        if cached_response:
            logger.debug(json.loads(cached_response))
            return json.loads(cached_response)
    try_times = 0
    while try_times < max_times:
        if try_times > 0:
            logger.debug(f"Try again... ({try_times+1}-th time)")
        try:
            response = request_api(model_name, user_prompt, system_prompt)
            if "error" in response.keys():
                # throw exception
                raise ResponseException(response["error"])
            if "detail" in response.keys() and "This model's maximum context length is 32768 tokens. However, you requested " in response["detail"]:
                raise ResponseException({"code": "context_length_exceeded", "_message": response["detail"]})
            if use_cache:
                r.setex(cache_key, 60*60*24*365, json.dumps(response))
            break
        except Exception as e:
            logger.error(f"{try_times+1}-th time Error:{e}, Type(e):{type(e)}")
            if (isinstance(e, openai.error.InvalidRequestError) or isinstance(e, ResponseException)) and e.code == "context_length_exceeded":
                logger.error(e.code)
                logger.error(e._message)
                token_num = int(e._message.split("However, you requested ")[1].split(" tokens.")[0])
                logger.error(f"Token number: {token_num}")
                if token_num > 8192 and model_name == "gpt-4-1106-preview":
                    if token_num > 128000 - 4096:
                        enc = tiktoken.encoding_for_model(model_name)
                        length_system_prompt = len(enc.encode(system_prompt))
                        user_prompt = enc.decode(enc.encode(user_prompt)[:128000 - 4096 - length_system_prompt])
            elif (isinstance(e, openai.error.InvalidRequestError) or isinstance(e, ResponseException)) and e.code == "rate_limit_exceeded":
                time_to_wait = e.message.split("Please try again in ")[1].split("s.")[0]
                if 'm' in time_to_wait:
                    time_to_wait = float(time_to_wait.split("m")[0])*60 + float(time_to_wait.split("m")[1].split("s")[0])
                else:
                    time_to_wait = float(time_to_wait.split("s")[0])
                time.sleep(time_to_wait)
            else:
                time.sleep(30)
            try_times += 1
    return response


def chat(system_prompt, user_prompt, model_name, output_format = None, use_cache=False, remove_quote=False,
         db_manager=None, request_index=None):
    response = atomic_chat(system_prompt, user_prompt, model_name=model_name, use_cache=use_cache)

    if db_manager is not None and request_index is not None:
        db_manager.add_response(request_index, response)
    else:
        logger.debug(response)
    try:
        print(response)
        if "choices" not in response.keys() and "messages" in response.keys():
            response_txt = response['messages'][-1]['content']
        elif "choices" in response.keys() and len(response['choices']) > 0:
            response_txt = response['choices'][0]['message']['content']
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
        if (response_txt[0] == "\'" and response_txt[-1] == "\'") or \
            (response_txt[0] == "\"" and response_txt[-1] == "\""):
            response_txt = response_txt[1:-1]
        if response_txt == "":
            response_txt = "<NONE>"
    return response_txt
