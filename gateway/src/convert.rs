//! Responses API <-> OpenAI Chat Completions 协议转换。
//!
//! 语义参照 ccx（backend-go/internal/converters）：
//! - 请求转换：responses -> chat、chat -> responses；
//! - 非流式响应转换：chat -> responses、responses -> chat；
//! - 流式转换：chat SSE -> responses SSE（增量状态机，含
//!   reasoning / text / function_call 分块与 usage 汇总），
//!   responses SSE -> chat SSE（增量转 chunk + [DONE]）。
//!
//! 所有函数都是纯函数 / 有界状态机，不依赖任何 I/O。

use crate::sse::{SseEvent, sse_line};
use serde_json::{Map, Value, json};
use std::time::{SystemTime, UNIX_EPOCH};

fn unix_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn now_nanos() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
}

// =====================================================================
// 请求转换
// =====================================================================

/// Responses 请求 -> Chat Completions 请求。
/// `upstream_model` 是渠道映射后的上游模型名。
pub fn responses_to_chat_request(body: &Value, upstream_model: &str, stream: bool) -> Value {
    let mut out = Map::new();
    out.insert("model".to_owned(), Value::String(upstream_model.to_owned()));
    let mut messages = Vec::<Value>::new();
    out.insert("messages".to_owned(), Value::Array(messages.clone()));
    out.insert("stream".to_owned(), Value::Bool(stream));
    if stream {
        out.insert("stream_options".to_owned(), json!({"include_usage": true}));
    }

    if let Some(max_tokens) = body.get("max_output_tokens").and_then(Value::as_u64) {
        out.insert("max_tokens".to_owned(), Value::from(max_tokens));
    }
    copy_number(body, &mut out, "temperature");
    copy_number(body, &mut out, "top_p");
    copy_string(body, &mut out, "user");

    // instructions -> system message
    if let Some(instructions) = body.get("instructions").and_then(Value::as_str)
        && !instructions.is_empty()
    {
        messages.push(json!({"role": "system", "content": instructions}));
    }

    // input -> messages
    match body.get("input") {
        Some(Value::String(text)) => {
            messages.push(json!({"role": "user", "content": text}));
        }
        Some(Value::Array(items)) => {
            for item in items {
                let item_type = item
                    .get("type")
                    .and_then(Value::as_str)
                    .or_else(|| {
                        item.get("role")
                            .and_then(Value::as_str)
                            .filter(|_| true)
                            .map(|_| "message")
                    })
                    .unwrap_or("message");
                match item_type {
                    "message" => {
                        if let Some(message) = responses_message_to_chat(item) {
                            messages.push(message);
                        }
                    }
                    "function_call" => {
                        messages.push(responses_function_call_to_chat(item));
                    }
                    "function_call_output" => {
                        messages.push(responses_function_output_to_chat(item));
                    }
                    _ => {}
                }
            }
        }
        _ => {}
    }
    out.insert("messages".to_owned(), Value::Array(messages));

    // tools -> function tools
    let has_tools = body.get("tools").is_some_and(Value::is_array);
    if has_tools {
        let tools = body
            .get("tools")
            .and_then(Value::as_array)
            .map(|tools| {
                tools
                    .iter()
                    .filter_map(responses_tool_to_chat)
                    .collect::<Vec<_>>()
            })
            .filter(|tools| !tools.is_empty());
        if let Some(tools) = tools {
            out.insert("tools".to_owned(), Value::Array(tools));
        }
    }
    // 仅当存在 tools 时才写入 tool_choice / parallel_tool_calls，避免上游拒绝。
    if out.contains_key("tools") {
        if let Some(value) = body.get("tool_choice") {
            out.insert("tool_choice".to_owned(), value.clone());
        }
        if let Some(value) = body.get("parallel_tool_calls") {
            out.insert("parallel_tool_calls".to_owned(), value.clone());
        }
    }

    // reasoning.effort -> reasoning_effort
    if let Some(effort) = body
        .get("reasoning")
        .and_then(|reasoning| reasoning.get("effort"))
        .and_then(Value::as_str)
    {
        let mapped = match effort {
            "minimal" => "low",
            other => other,
        };
        out.insert(
            "reasoning_effort".to_owned(),
            Value::String(mapped.to_owned()),
        );
    }

    Value::Object(out)
}

fn copy_number(body: &Value, out: &mut Map<String, Value>, key: &str) {
    if let Some(value) = body.get(key).and_then(Value::as_f64) {
        out.insert(key.to_owned(), Value::from(value));
    }
}

fn copy_string(body: &Value, out: &mut Map<String, Value>, key: &str) {
    if let Some(value) = body.get(key).and_then(Value::as_str) {
        out.insert(key.to_owned(), Value::String(value.to_owned()));
    }
}

fn responses_message_to_chat(item: &Value) -> Option<Value> {
    let role = item.get("role").and_then(Value::as_str).unwrap_or("user");
    let content = match item.get("content") {
        Some(Value::String(text)) => Value::String(text.clone()),
        Some(Value::Array(parts)) => {
            let mut text_parts = Vec::new();
            let mut blocks = Vec::<Value>::new();
            let mut has_media = false;
            for part in parts {
                let part_type = part
                    .get("type")
                    .and_then(Value::as_str)
                    .unwrap_or("input_text");
                match part_type {
                    "input_text" | "output_text" | "text" => {
                        if let Some(text) = part.get("text").and_then(Value::as_str) {
                            text_parts.push(text.to_owned());
                            blocks.push(json!({"type": "text", "text": text}));
                        }
                    }
                    "input_image" | "image_url" => {
                        if let Some(block) = responses_image_to_chat(part) {
                            blocks.push(block);
                            has_media = true;
                        }
                    }
                    _ => {}
                }
            }
            if has_media {
                Value::Array(blocks)
            } else if text_parts.is_empty() {
                Value::String(String::new())
            } else {
                Value::String(text_parts.join("\n"))
            }
        }
        _ => Value::String(String::new()),
    };
    Some(json!({"role": role, "content": content}))
}

fn responses_image_to_chat(part: &Value) -> Option<Value> {
    // 形式一：image_url 字段（string 或 {url, detail}）
    if let Some(image_url) = part.get("image_url") {
        let url = match image_url {
            Value::String(url) if !url.is_empty() => url.clone(),
            Value::Object(map) => map.get("url").and_then(Value::as_str)?.to_owned(),
            _ => String::new(),
        };
        if !url.is_empty() {
            let mut block = json!({"type": "image_url", "image_url": {"url": url}});
            if let Some(detail) = part.get("detail").and_then(Value::as_str) {
                block["image_url"]["detail"] = Value::String(detail.to_owned());
            }
            return Some(block);
        }
    }
    // 形式二：source 字段（url / base64）
    if let Some(source) = part.get("source") {
        let source_type = source.get("type").and_then(Value::as_str)?;
        let url = match source_type {
            "url" => source.get("url").and_then(Value::as_str)?.to_owned(),
            "base64" => {
                let media_type = source.get("media_type").and_then(Value::as_str)?;
                let data = source.get("data").and_then(Value::as_str)?;
                format!("data:{media_type};base64,{data}")
            }
            _ => return None,
        };
        if !url.is_empty() {
            return Some(json!({"type": "image_url", "image_url": {"url": url}}));
        }
    }
    None
}

fn responses_function_call_to_chat(item: &Value) -> Value {
    let mut tool_call = json!({"type": "function", "function": {"name": "", "arguments": ""}});
    if let Some(call_id) = item.get("call_id").and_then(Value::as_str) {
        tool_call["id"] = Value::String(call_id.to_owned());
        tool_call["function"]["name"] = item
            .get("name")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .unwrap_or_default()
            .into();
        tool_call["function"]["arguments"] = item
            .get("arguments")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .unwrap_or_default()
            .into();
    }
    json!({"role": "assistant", "tool_calls": [tool_call]})
}

fn responses_function_output_to_chat(item: &Value) -> Value {
    let mut message = json!({"role": "tool", "tool_call_id": "", "content": ""});
    if let Some(call_id) = item.get("call_id").and_then(Value::as_str) {
        message["tool_call_id"] = Value::String(call_id.to_owned());
    }
    if let Some(output) = item.get("output") {
        message["content"] = match output {
            Value::String(text) => Value::String(text.clone()),
            other => other.clone(),
        };
    }
    message
}

/// Responses tool -> Chat function tool；非 function 工具跳过。
fn responses_tool_to_chat(tool: &Value) -> Option<Value> {
    let tool_type = tool.get("type").and_then(Value::as_str).unwrap_or("");
    if !tool_type.is_empty() && tool_type != "function" {
        return None;
    }
    // 支持 {name, parameters} 与 {function: {name, ...}} 两种写法。
    let name = tool.get("name").and_then(Value::as_str).or_else(|| {
        tool.get("function")
            .and_then(|f| f.get("name"))
            .and_then(Value::as_str)
    })?;
    if name.is_empty() {
        return None;
    }
    let description = tool
        .get("description")
        .and_then(Value::as_str)
        .or_else(|| {
            tool.get("function")
                .and_then(|f| f.get("description"))
                .and_then(Value::as_str)
        })
        .unwrap_or_default();
    let parameters = tool
        .get("parameters")
        .or_else(|| tool.get("function").and_then(|f| f.get("parameters")))
        .cloned()
        .unwrap_or_else(|| json!({}));
    Some(json!({
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": normalize_tool_parameters(parameters),
        }
    }))
}

/// 补齐严格校验上游要求的 JSON Schema 字段（type/properties/required）。
fn normalize_tool_parameters(parameters: Value) -> Value {
    let mut object = match parameters {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    if !object.contains_key("type") {
        object.insert("type".to_owned(), Value::String("object".to_owned()));
    }
    if !object.contains_key("properties") {
        object.insert("properties".to_owned(), json!({}));
    }
    if !object.contains_key("required") {
        object.insert("required".to_owned(), json!([]));
    }
    Value::Object(object)
}

/// Chat Completions 请求 -> Responses 请求。
pub fn chat_to_responses_request(body: &Value, upstream_model: &str) -> Value {
    let mut out = Map::new();
    out.insert("model".to_owned(), Value::String(upstream_model.to_owned()));
    out.insert("input".to_owned(), Value::Array(Vec::new()));
    out.insert(
        "stream".to_owned(),
        body.get("stream")
            .and_then(Value::as_bool)
            .unwrap_or(false)
            .into(),
    );

    // system 消息合并为 instructions
    let mut instructions = Vec::<String>::new();
    let mut input = Vec::<Value>::new();
    if let Some(messages) = body.get("messages").and_then(Value::as_array) {
        for message in messages {
            let role = message
                .get("role")
                .and_then(Value::as_str)
                .unwrap_or("user");
            match role {
                "system" => {
                    if let Some(content) = message.get("content") {
                        match content {
                            Value::String(text) => instructions.push(text.clone()),
                            Value::Array(blocks) => {
                                for block in blocks {
                                    if let Some(text) = block.get("text").and_then(Value::as_str) {
                                        instructions.push(text.to_owned());
                                    }
                                }
                            }
                            _ => {}
                        }
                    }
                }
                "user" => input.push(chat_user_message_to_responses(message)),
                "assistant" => input.extend(chat_assistant_message_to_responses(message)),
                "tool" => input.push(chat_tool_message_to_responses(message)),
                _ => {}
            }
        }
    }
    if !instructions.is_empty() {
        out.insert(
            "instructions".to_owned(),
            Value::String(instructions.join("\n")),
        );
    }
    out.insert("input".to_owned(), Value::Array(input));

    // max_tokens / max_completion_tokens -> max_output_tokens
    let max_tokens = body
        .get("max_tokens")
        .and_then(Value::as_u64)
        .or_else(|| body.get("max_completion_tokens").and_then(Value::as_u64));
    if let Some(max_tokens) = max_tokens {
        out.insert("max_output_tokens".to_owned(), Value::from(max_tokens));
    }
    copy_number(body, &mut out, "temperature");
    copy_number(body, &mut out, "top_p");
    copy_string(body, &mut out, "user");

    // tools -> responses 格式 {type: function, name, description, parameters}
    if let Some(tools) = body.get("tools").and_then(Value::as_array) {
        let converted = tools
            .iter()
            .filter(|tool| {
                tool.get("type")
                    .and_then(Value::as_str)
                    .unwrap_or("function")
                    == "function"
            })
            .filter_map(|tool| {
                let function = tool.get("function")?;
                let name = function.get("name").and_then(Value::as_str)?;
                let mut converted = Map::new();
                converted.insert("type".to_owned(), Value::String("function".to_owned()));
                converted.insert("name".to_owned(), Value::String(name.to_owned()));
                if let Some(description) = function.get("description").and_then(Value::as_str) {
                    converted.insert(
                        "description".to_owned(),
                        Value::String(description.to_owned()),
                    );
                }
                if let Some(parameters) = function.get("parameters") {
                    converted.insert("parameters".to_owned(), parameters.clone());
                }
                Some(Value::Object(converted))
            })
            .collect::<Vec<_>>();
        if !converted.is_empty() {
            out.insert("tools".to_owned(), Value::Array(converted));
        }
    }
    if let Some(value) = body.get("tool_choice") {
        out.insert("tool_choice".to_owned(), value.clone());
    }
    if let Some(value) = body.get("parallel_tool_calls") {
        out.insert("parallel_tool_calls".to_owned(), value.clone());
    }

    // reasoning_effort -> reasoning.effort（保留原始 reasoning 的其它字段）
    if let Some(effort) = body.get("reasoning_effort").and_then(Value::as_str) {
        let mut reasoning = match body.get("reasoning") {
            Some(Value::Object(map)) => map.clone(),
            _ => Map::new(),
        };
        reasoning.insert("effort".to_owned(), Value::String(effort.to_owned()));
        out.insert("reasoning".to_owned(), Value::Object(reasoning));
    } else if let Some(reasoning) = body.get("reasoning") {
        out.insert("reasoning".to_owned(), reasoning.clone());
    }

    Value::Object(out)
}

fn chat_user_message_to_responses(message: &Value) -> Value {
    let mut item = json!({"type": "message", "role": "user", "content": []});
    match message.get("content") {
        Some(Value::String(text)) => {
            item["content"] = json!([{"type": "input_text", "text": text}]);
        }
        Some(Value::Array(blocks)) => {
            let mut content = Vec::new();
            for block in blocks {
                match block.get("type").and_then(Value::as_str) {
                    Some("text") => {
                        if let Some(text) = block.get("text").and_then(Value::as_str) {
                            content.push(json!({"type": "input_text", "text": text}));
                        }
                    }
                    Some("image_url") => {
                        if let Some(image) = chat_image_to_responses(block) {
                            content.push(image);
                        }
                    }
                    _ => {}
                }
            }
            item["content"] = Value::Array(content);
        }
        _ => {}
    }
    item
}

fn chat_image_to_responses(block: &Value) -> Option<Value> {
    let image_url = block.get("image_url")?;
    match image_url {
        Value::String(url) => Some(json!({"type": "input_image", "image_url": url})),
        Value::Object(map) => {
            let url = map.get("url").and_then(Value::as_str)?;
            let mut image = json!({"type": "input_image", "image_url": url});
            if let Some(detail) = map.get("detail").and_then(Value::as_str) {
                image["detail"] = Value::String(detail.to_owned());
            }
            Some(image)
        }
        _ => None,
    }
}

fn chat_assistant_message_to_responses(message: &Value) -> Vec<Value> {
    let mut items = Vec::new();
    // 文本 -> message item
    let mut content = Vec::new();
    match message.get("content") {
        Some(Value::String(text)) if !text.is_empty() => {
            content.push(json!({"type": "output_text", "text": text}));
        }
        Some(Value::Array(blocks)) => {
            for block in blocks {
                if let Some("text" | "input_text" | "output_text") =
                    block.get("type").and_then(Value::as_str)
                    && let Some(text) = block.get("text").and_then(Value::as_str)
                {
                    content.push(json!({"type": "output_text", "text": text}));
                }
            }
        }
        _ => {}
    }
    if !content.is_empty() {
        items.push(json!({"type": "message", "role": "assistant", "content": content}));
    }
    // tool_calls -> function_call items
    if let Some(tool_calls) = message.get("tool_calls").and_then(Value::as_array) {
        for tool_call in tool_calls {
            let call_id = tool_call
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let name = tool_call
                .pointer("/function/name")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let arguments = tool_call
                .pointer("/function/arguments")
                .and_then(Value::as_str)
                .filter(|args| !args.is_empty())
                .unwrap_or("{}");
            items.push(json!({
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            }));
        }
    }
    // 空 assistant 消息也保留，维持对话结构。
    if items.is_empty() {
        items.push(json!({"type": "message", "role": "assistant", "content": []}));
    }
    items
}

fn chat_tool_message_to_responses(message: &Value) -> Value {
    let call_id = message
        .get("tool_call_id")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let output = match message.get("content") {
        Some(Value::String(text)) => Value::String(text.clone()),
        Some(other) => other.clone(),
        None => Value::String(String::new()),
    };
    json!({"type": "function_call_output", "call_id": call_id, "output": output})
}

// =====================================================================
// 非流式响应转换
// =====================================================================

/// Chat 响应 -> Responses 响应（非流式）。
/// `original_request` 是客户端原始的 Responses 请求，用于回显字段。
pub fn chat_to_responses_nonstream(chat: &Value, original_request: &Value) -> Value {
    let mut response = json!({
        "id": chat.get("id").and_then(Value::as_str).unwrap_or("resp_compat"),
        "object": "response",
        "created_at": chat.get("created").and_then(Value::as_u64).unwrap_or_else(unix_seconds),
        "status": "completed",
        "background": false,
        "error": null,
        "incomplete_details": null,
        "output": [],
        "usage": {
            "input_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {},
            "total_tokens": 0,
        },
    });
    let model = original_request
        .get("model")
        .and_then(Value::as_str)
        .map(str::to_owned);
    if let Some(model) = model {
        response["model"] = Value::String(model);
    }
    // 回显客户端请求字段
    for key in [
        "instructions",
        "max_output_tokens",
        "parallel_tool_calls",
        "previous_response_id",
        "reasoning",
        "temperature",
        "tool_choice",
        "tools",
        "top_p",
        "metadata",
    ] {
        if let Some(value) = original_request.get(key) {
            response[key] = value.clone();
        }
    }

    let mut outputs = Vec::<Value>::new();
    let mut reasoning_buf = String::new();
    let mut text_buf = String::new();
    let mut tool_calls = Vec::<Value>::new();
    let response_id = response["id"].as_str().unwrap_or("resp_compat").to_owned();

    if let Some(choices) = chat.get("choices").and_then(Value::as_array) {
        for choice in choices {
            let Some(message) = choice.get("message") else {
                continue;
            };
            // reasoning_content / reasoning -> reasoning item
            let reasoning = message
                .get("reasoning_content")
                .and_then(Value::as_str)
                .or_else(|| message.get("reasoning").and_then(Value::as_str));
            if let Some(reasoning) = reasoning {
                reasoning_buf.push_str(reasoning);
            }
            // content：<think> 标签提取到 reasoning
            if let Some(content) = message.get("content").and_then(Value::as_str) {
                let (thinking, remaining) = extract_think_tag(content);
                if !thinking.is_empty() {
                    if !reasoning_buf.is_empty() {
                        reasoning_buf.push('\n');
                    }
                    reasoning_buf.push_str(&thinking);
                }
                if !remaining.is_empty() {
                    if !text_buf.is_empty() {
                        text_buf.push('\n');
                    }
                    text_buf.push_str(&remaining);
                }
            }
            if let Some(calls) = message.get("tool_calls").and_then(Value::as_array) {
                for (index, call) in calls.iter().enumerate() {
                    tool_calls.push(json!({
                        "index": index,
                        "id": call.get("id").and_then(Value::as_str).unwrap_or_default(),
                        "type": "function",
                        "function": {
                            "name": call.pointer("/function/name").and_then(Value::as_str).unwrap_or_default(),
                            "arguments": call.pointer("/function/arguments").and_then(Value::as_str).unwrap_or("{}"),
                        }
                    }));
                }
            }
        }
    }

    let mut output_index = 0usize;
    if !reasoning_buf.is_empty() {
        outputs.push(json!({
            "id": format!("rs_{response_id}_0"),
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": reasoning_buf}],
        }));
        output_index += 1;
    }
    if !text_buf.is_empty() {
        outputs.push(json!({
            "id": format!("msg_{response_id}_{output_index}"),
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "annotations": [], "logprobs": [], "text": text_buf}],
        }));
    }
    for call in &tool_calls {
        let call_id = call["id"].as_str().unwrap_or_default();
        let name = call
            .pointer("/function/name")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let arguments = call
            .pointer("/function/arguments")
            .and_then(Value::as_str)
            .unwrap_or("{}");
        outputs.push(json!({
            "id": format!("fc_{call_id}"),
            "type": "function_call",
            "status": "completed",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
        }));
    }
    if !outputs.is_empty() {
        response["output"] = Value::Array(outputs);
    }
    response["output_text"] = Value::String(text_buf.clone());

    // usage
    if let Some(usage) = chat.get("usage") {
        let input_tokens = usage
            .get("prompt_tokens")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let output_tokens = usage
            .get("completion_tokens")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let total_tokens = usage
            .get("total_tokens")
            .and_then(Value::as_u64)
            .unwrap_or(input_tokens + output_tokens);
        response["usage"]["input_tokens"] = Value::from(input_tokens);
        response["usage"]["output_tokens"] = Value::from(output_tokens);
        response["usage"]["total_tokens"] = Value::from(total_tokens);
        if let Some(cached) = usage
            .pointer("/prompt_tokens_details/cached_tokens")
            .and_then(Value::as_u64)
            && cached > 0
        {
            response["usage"]["input_tokens_details"]["cached_tokens"] = Value::from(cached);
        }
        let reasoning_tokens = usage
            .pointer("/completion_tokens_details/reasoning_tokens")
            .and_then(Value::as_u64)
            .unwrap_or({
                if reasoning_buf.is_empty() {
                    0
                } else {
                    (reasoning_buf.len() / 4) as u64
                }
            });
        if reasoning_tokens > 0 {
            response["usage"]["output_tokens_details"]["reasoning_tokens"] =
                Value::from(reasoning_tokens);
        }
    }
    response
}

/// Responses 响应 -> Chat 响应（非流式）。
pub fn responses_to_chat_nonstream(responses: &Value, model: &str) -> Value {
    let mut message = json!({"role": "assistant", "content": null});
    let mut text_parts = Vec::<String>::new();
    let mut reasoning_parts = Vec::<String>::new();
    let mut tool_calls = Vec::<Value>::new();
    let mut has_function_call = false;
    let mut status = String::new();

    if let Some(output) = responses.get("output").and_then(Value::as_array) {
        for item in output {
            match item.get("type").and_then(Value::as_str) {
                Some("message") => {
                    if let Some(content) = item.get("content").and_then(Value::as_array) {
                        for block in content {
                            if matches!(
                                block.get("type").and_then(Value::as_str),
                                Some("output_text" | "input_text" | "text")
                            ) && let Some(text) = block.get("text").and_then(Value::as_str)
                            {
                                text_parts.push(text.to_owned());
                            }
                        }
                    }
                }
                Some("reasoning") => {
                    if let Some(summary) = item.get("summary").and_then(Value::as_array) {
                        for block in summary {
                            if let Some(text) = block.get("text").and_then(Value::as_str) {
                                reasoning_parts.push(text.to_owned());
                            }
                        }
                    }
                }
                Some("function_call") => {
                    has_function_call = true;
                    let call_id = item
                        .get("call_id")
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    let name = item.get("name").and_then(Value::as_str).unwrap_or_default();
                    let arguments = item
                        .get("arguments")
                        .and_then(Value::as_str)
                        .filter(|args| !args.is_empty())
                        .unwrap_or("{}");
                    tool_calls.push(json!({
                        "index": tool_calls.len(),
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }));
                }
                _ => {}
            }
        }
    }
    if !text_parts.is_empty() {
        message["content"] = Value::String(text_parts.join("\n"));
    }
    if !reasoning_parts.is_empty() {
        message["reasoning_content"] = Value::String(reasoning_parts.join("\n"));
    }
    if !tool_calls.is_empty() {
        message["tool_calls"] = Value::Array(tool_calls);
    }

    let finish_reason = if has_function_call {
        "tool_calls"
    } else if responses
        .get("status")
        .and_then(Value::as_str)
        .is_some_and(|value| value == "incomplete")
    {
        status = "incomplete".to_owned();
        "length"
    } else {
        "stop"
    };

    let mut response = json!({
        "id": responses.get("id").and_then(Value::as_str).unwrap_or("resp_compat"),
        "object": "chat.completion",
        "created": responses.get("created_at").and_then(Value::as_u64).unwrap_or_else(unix_seconds),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    });
    if let Some(usage) = responses.get("usage") {
        let input_tokens = usage
            .get("input_tokens")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let output_tokens = usage
            .get("output_tokens")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let total_tokens = usage
            .get("total_tokens")
            .and_then(Value::as_u64)
            .unwrap_or(input_tokens + output_tokens);
        response["usage"] = json!({
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": total_tokens,
        });
    }
    let _ = status;
    response
}

/// 从 content 开头提取 `<think>...</think>` 片段。
/// 返回 (thinking, remaining)。
pub fn extract_think_tag(content: &str) -> (String, String) {
    let trimmed = content.trim_start();
    let Some(open) = trimmed.find("<think>") else {
        return (String::new(), content.to_owned());
    };
    if open > 0 {
        return (String::new(), content.to_owned());
    }
    let after_open = &trimmed["<think>".len()..];
    let Some(close) = after_open.find("</think>") else {
        return (String::new(), content.to_owned());
    };
    (
        after_open[..close].to_owned(),
        after_open[close + "</think>".len()..].to_owned(),
    )
}

// =====================================================================
// 流式转换：Chat SSE -> Responses SSE
// =====================================================================

/// <think> 标签增量切分器：把流式 content 中完整的 <think>...</think>
/// 提取到 reasoning 通道，其余进入 content 通道；跨 chunk 的半截标签安全缓冲。
#[derive(Default)]
struct ThinkSplitter {
    buf: String,
    in_think: bool,
}

impl ThinkSplitter {
    fn feed(&mut self, text: &str) -> (String, String) {
        self.buf.push_str(text);
        let mut reasoning = String::new();
        let mut content = String::new();
        loop {
            if self.in_think {
                match self.buf.find("</think>") {
                    Some(pos) => {
                        reasoning.push_str(&self.buf[..pos]);
                        self.buf.drain(..pos + "</think>".len());
                        self.in_think = false;
                    }
                    None => {
                        let keep = partial_tag_len(&self.buf, "</think>");
                        let flush_len = self.buf.len() - keep;
                        reasoning.push_str(&self.buf[..flush_len]);
                        self.buf.drain(..flush_len);
                        break;
                    }
                }
            } else {
                match self.buf.find("<think>") {
                    Some(pos) => {
                        content.push_str(&self.buf[..pos]);
                        self.buf.drain(..pos + "<think>".len());
                        self.in_think = true;
                    }
                    None => {
                        let keep = partial_tag_len(&self.buf, "<think>");
                        let flush_len = self.buf.len() - keep;
                        content.push_str(&self.buf[..flush_len]);
                        self.buf.drain(..flush_len);
                        break;
                    }
                }
            }
        }
        (reasoning, content)
    }

    fn drain(&mut self) -> (String, String) {
        let text = std::mem::take(&mut self.buf);
        if self.in_think {
            (text, String::new())
        } else {
            (String::new(), text)
        }
    }
}

/// 若 buf 以 tag 的部分前缀结尾，返回需要保留的字节数。
fn partial_tag_len(buf: &str, tag: &str) -> usize {
    for len in (1..tag.len()).rev() {
        if buf.ends_with(&tag[..len]) {
            return len;
        }
    }
    0
}

#[derive(Default)]
struct FuncCallState {
    call_id: String,
    name: String,
    args: String,
    item_added: bool,
}

/// Chat SSE -> Responses SSE 增量转换器。
pub struct ChatToResponsesStreamer {
    original_request: Value,
    seq: u64,
    response_id: String,
    created_at: u64,
    current_msg_id: String,
    in_text_block: bool,
    text_buf: String,
    reasoning_active: bool,
    reasoning_item_id: String,
    reasoning_buf: String,
    reasoning_part_added: bool,
    reasoning_index: usize,
    funcs: std::collections::BTreeMap<usize, FuncCallState>,
    in_func_block: bool,
    input_tokens: u64,
    output_tokens: u64,
    total_tokens: u64,
    cached_tokens: u64,
    reasoning_tokens: u64,
    first_chunk: bool,
    think: ThinkSplitter,
}

impl ChatToResponsesStreamer {
    pub fn new(original_request: Value) -> Self {
        Self {
            original_request,
            seq: 0,
            response_id: String::new(),
            created_at: 0,
            current_msg_id: String::new(),
            in_text_block: false,
            text_buf: String::new(),
            reasoning_active: false,
            reasoning_item_id: String::new(),
            reasoning_buf: String::new(),
            reasoning_part_added: false,
            reasoning_index: 0,
            funcs: std::collections::BTreeMap::new(),
            in_func_block: false,
            input_tokens: 0,
            output_tokens: 0,
            total_tokens: 0,
            cached_tokens: 0,
            reasoning_tokens: 0,
            first_chunk: true,
            think: ThinkSplitter::default(),
        }
    }

    fn next_seq(&mut self) -> u64 {
        self.seq += 1;
        self.seq
    }

    /// 处理一个 Chat chunk（choices[0] 的 delta），返回 Responses SSE 行。
    pub fn feed(&mut self, chunk: &Value) -> Vec<String> {
        let mut out = Vec::new();
        if self.first_chunk {
            self.first_chunk = false;
            self.response_id = chunk
                .get("id")
                .and_then(Value::as_str)
                .filter(|id| !id.is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| format!("resp_{}", now_nanos()));
            self.created_at = unix_seconds();
            out.push(self.created_event());
            out.push(self.in_progress_event());
        }
        let Some(choices) = chunk.get("choices").and_then(Value::as_array) else {
            // usage-only chunk
            self.collect_usage(chunk);
            return out;
        };
        for choice in choices {
            let delta = choice.get("delta");
            let finish_reason = choice.get("finish_reason").and_then(Value::as_str);
            if let Some(delta) = delta {
                // reasoning_content（DeepSeek/OpenAI）或 reasoning（vLLM）
                let reasoning = delta
                    .get("reasoning_content")
                    .and_then(Value::as_str)
                    .filter(|text| !text.is_empty())
                    .or_else(|| {
                        delta
                            .get("reasoning")
                            .and_then(Value::as_str)
                            .filter(|text| !text.is_empty())
                    });
                if let Some(reasoning) = reasoning {
                    out.extend(self.handle_reasoning(reasoning));
                }
                if let Some(content) = delta.get("content").and_then(Value::as_str)
                    && !content.is_empty()
                {
                    let (thinking, text) = self.think.feed(content);
                    if !thinking.is_empty() {
                        out.extend(self.handle_reasoning(&thinking));
                    }
                    if !text.is_empty() {
                        out.extend(self.handle_content(&text));
                    }
                }
                if let Some(tool_calls) = delta.get("tool_calls").and_then(Value::as_array) {
                    for tool_call in tool_calls {
                        let index =
                            tool_call.get("index").and_then(Value::as_u64).unwrap_or(0) as usize;
                        // 开始新工具调用前关闭打开的块
                        if self.reasoning_active {
                            out.extend(self.close_reasoning());
                        }
                        if self.in_text_block {
                            out.extend(self.close_text());
                        }
                        self.in_func_block = true;
                        // 先从 chunk 提取数据，避免跨方法调用持有 funcs 借用。
                        let new_call_id = tool_call
                            .get("id")
                            .and_then(Value::as_str)
                            .map(str::to_owned)
                            .unwrap_or_default();
                        let new_name = tool_call
                            .pointer("/function/name")
                            .and_then(Value::as_str)
                            .map(str::to_owned)
                            .unwrap_or_default();
                        let args_delta = tool_call
                            .pointer("/function/arguments")
                            .and_then(Value::as_str)
                            .unwrap_or_default();
                        let (call_id, need_item) = {
                            let state = self.funcs.entry(index).or_default();
                            if !new_call_id.is_empty() && state.call_id.is_empty() {
                                state.call_id = new_call_id;
                            }
                            let call_id = state.call_id.clone();
                            if !state.item_added && !call_id.is_empty() {
                                state.item_added = true;
                                (call_id, true)
                            } else {
                                (call_id, false)
                            }
                        };
                        if need_item {
                            out.push(self.function_item_added(index, &call_id));
                        }
                        if !new_name.is_empty() {
                            let state = self.funcs.entry(index).or_default();
                            if state.name.is_empty() {
                                state.name = new_name;
                            }
                        }
                        if !args_delta.is_empty() {
                            let call_id = {
                                let state = self.funcs.entry(index).or_default();
                                state.args.push_str(args_delta);
                                state.call_id.clone()
                            };
                            out.push(self.function_args_delta(index, args_delta, &call_id));
                        }
                    }
                }
            }
            // finish_reason：先刷 think 缓冲，再关闭所有块
            if let Some(reason) = finish_reason
                && !reason.is_empty()
                && reason != "null"
            {
                let (thinking, text) = self.think.drain();
                if !thinking.is_empty() {
                    out.extend(self.handle_reasoning(&thinking));
                }
                if !text.is_empty() {
                    out.extend(self.handle_content(&text));
                }
                if self.reasoning_active {
                    out.extend(self.close_reasoning());
                }
                if self.in_text_block {
                    out.extend(self.close_text());
                }
                if self.in_func_block {
                    out.extend(self.close_funcs());
                }
            }
        }
        self.collect_usage(chunk);
        out
    }

    fn collect_usage(&mut self, chunk: &Value) {
        let Some(usage) = chunk.get("usage") else {
            return;
        };
        if let Some(value) = usage.get("prompt_tokens").and_then(Value::as_u64) {
            self.input_tokens = value;
        }
        if let Some(value) = usage.get("completion_tokens").and_then(Value::as_u64) {
            self.output_tokens = value;
        }
        if let Some(value) = usage.get("total_tokens").and_then(Value::as_u64) {
            self.total_tokens = value;
        }
        if let Some(value) = usage
            .pointer("/prompt_tokens_details/cached_tokens")
            .and_then(Value::as_u64)
        {
            self.cached_tokens = value;
        }
        if let Some(value) = usage
            .pointer("/completion_tokens_details/reasoning_tokens")
            .and_then(Value::as_u64)
        {
            self.reasoning_tokens = value;
        }
    }

    /// 上游流结束（[DONE] 或 EOF）：关闭所有块并发出 response.completed。
    pub fn finish(&mut self) -> Vec<String> {
        let mut out = Vec::new();
        let (thinking, text) = self.think.drain();
        if !thinking.is_empty() {
            out.extend(self.handle_reasoning(&thinking));
        }
        if !text.is_empty() {
            out.extend(self.handle_content(&text));
        }
        if self.reasoning_active {
            out.extend(self.close_reasoning());
        }
        if self.in_text_block {
            out.extend(self.close_text());
        }
        if self.in_func_block {
            out.extend(self.close_funcs());
        }
        out.push(self.completed_event());
        out
    }

    // ---- 事件构造 ----

    fn created_event(&mut self) -> String {
        sse_line(
            Some("response.created"),
            &json!({
                "type": "response.created",
                "sequence_number": self.next_seq(),
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "created_at": self.created_at,
                    "status": "in_progress",
                    "background": false,
                    "error": null,
                    "instructions": "",
                }
            })
            .to_string(),
        )
    }

    fn in_progress_event(&mut self) -> String {
        sse_line(
            Some("response.in_progress"),
            &json!({
                "type": "response.in_progress",
                "sequence_number": self.next_seq(),
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "created_at": self.created_at,
                    "status": "in_progress",
                }
            })
            .to_string(),
        )
    }

    fn handle_reasoning(&mut self, text: &str) -> Vec<String> {
        if text.is_empty() {
            return Vec::new();
        }
        let mut out = Vec::new();
        if !self.reasoning_active {
            self.reasoning_active = true;
            self.reasoning_index = 0;
            self.reasoning_buf.clear();
            self.reasoning_item_id = format!("rs_{}_0", self.response_id);
            out.push(sse_line(
                Some("response.output_item.added"),
                &json!({
                    "type": "response.output_item.added",
                    "sequence_number": self.next_seq(),
                    "output_index": self.reasoning_index,
                    "item": {
                        "id": self.reasoning_item_id,
                        "type": "reasoning",
                        "status": "in_progress",
                        "summary": [],
                    }
                })
                .to_string(),
            ));
            out.push(sse_line(
                Some("response.reasoning_summary_part.added"),
                &json!({
                    "type": "response.reasoning_summary_part.added",
                    "sequence_number": self.next_seq(),
                    "item_id": self.reasoning_item_id,
                    "output_index": self.reasoning_index,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                })
                .to_string(),
            ));
            self.reasoning_part_added = true;
        }
        self.reasoning_buf.push_str(text);
        out.push(sse_line(
            Some("response.reasoning_summary_text.delta"),
            &json!({
                "type": "response.reasoning_summary_text.delta",
                "sequence_number": self.next_seq(),
                "item_id": self.reasoning_item_id,
                "output_index": self.reasoning_index,
                "summary_index": 0,
                "text": text,
            })
            .to_string(),
        ));
        out
    }

    fn close_reasoning(&mut self) -> Vec<String> {
        if !self.reasoning_active {
            return Vec::new();
        }
        // 注意：不消费缓冲，completed 事件还需要读取全文。
        let full = self.reasoning_buf.clone();
        let out = vec![
            sse_line(
                Some("response.reasoning_summary_text.done"),
                &json!({
                    "type": "response.reasoning_summary_text.done",
                    "sequence_number": self.next_seq(),
                    "item_id": self.reasoning_item_id,
                    "output_index": self.reasoning_index,
                    "summary_index": 0,
                    "text": full,
                })
                .to_string(),
            ),
            sse_line(
                Some("response.reasoning_summary_part.done"),
                &json!({
                    "type": "response.reasoning_summary_part.done",
                    "sequence_number": self.next_seq(),
                    "item_id": self.reasoning_item_id,
                    "output_index": self.reasoning_index,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": full},
                })
                .to_string(),
            ),
            sse_line(
                Some("response.output_item.done"),
                &json!({
                    "type": "response.output_item.done",
                    "sequence_number": self.next_seq(),
                    "output_index": self.reasoning_index,
                    "item": {
                        "id": self.reasoning_item_id,
                        "type": "reasoning",
                        "status": "completed",
                        "summary": [{"type": "summary_text", "text": full}],
                    }
                })
                .to_string(),
            ),
        ];
        self.reasoning_active = false;
        out
    }

    fn handle_content(&mut self, text: &str) -> Vec<String> {
        if text.is_empty() {
            return Vec::new();
        }
        let mut out = Vec::new();
        if self.reasoning_active {
            out.extend(self.close_reasoning());
        }
        let output_index = usize::from(self.reasoning_part_added);
        if !self.in_text_block {
            self.in_text_block = true;
            self.current_msg_id = format!("msg_{}_{}", self.response_id, output_index);
            out.push(sse_line(
                Some("response.output_item.added"),
                &json!({
                    "type": "response.output_item.added",
                    "sequence_number": self.next_seq(),
                    "output_index": output_index,
                    "item": {
                        "id": self.current_msg_id,
                        "type": "message",
                        "status": "in_progress",
                        "content": [],
                        "role": "assistant",
                    }
                })
                .to_string(),
            ));
            out.push(sse_line(
                Some("response.content_part.added"),
                &json!({
                    "type": "response.content_part.added",
                    "sequence_number": self.next_seq(),
                    "item_id": self.current_msg_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": ""},
                })
                .to_string(),
            ));
        }
        self.text_buf.push_str(text);
        out.push(sse_line(
            Some("response.output_text.delta"),
            &json!({
                "type": "response.output_text.delta",
                "sequence_number": self.next_seq(),
                "item_id": self.current_msg_id,
                "output_index": output_index,
                "content_index": 0,
                "delta": text,
                "logprobs": [],
            })
            .to_string(),
        ));
        out
    }

    fn close_text(&mut self) -> Vec<String> {
        if !self.in_text_block {
            return Vec::new();
        }
        // 注意：不消费缓冲，completed 事件还需要读取全文。
        let full = self.text_buf.clone();
        let output_index = usize::from(self.reasoning_part_added);
        let out = vec![
            sse_line(
                Some("response.output_text.done"),
                &json!({
                    "type": "response.output_text.done",
                    "sequence_number": self.next_seq(),
                    "item_id": self.current_msg_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "text": full,
                    "logprobs": [],
                })
                .to_string(),
            ),
            sse_line(
                Some("response.content_part.done"),
                &json!({
                    "type": "response.content_part.done",
                    "sequence_number": self.next_seq(),
                    "item_id": self.current_msg_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": full},
                })
                .to_string(),
            ),
            sse_line(
                Some("response.output_item.done"),
                &json!({
                    "type": "response.output_item.done",
                    "sequence_number": self.next_seq(),
                    "output_index": output_index,
                    "item": {
                        "id": self.current_msg_id,
                        "type": "message",
                        "status": "completed",
                        "content": [{"type": "output_text", "annotations": [], "logprobs": [], "text": full}],
                        "role": "assistant",
                    }
                })
                .to_string(),
            ),
        ];
        self.in_text_block = false;
        out
    }

    fn tool_output_index(&self, index: usize) -> usize {
        index
            + usize::from(self.reasoning_part_added)
            + usize::from(!self.current_msg_id.is_empty())
    }

    fn function_item_added(&mut self, index: usize, call_id: &str) -> String {
        sse_line(
            Some("response.output_item.added"),
            &json!({
                "type": "response.output_item.added",
                "sequence_number": self.next_seq(),
                "output_index": self.tool_output_index(index),
                "item": {
                    "id": format!("fc_{call_id}"),
                    "type": "function_call",
                    "status": "in_progress",
                    "arguments": "",
                    "call_id": call_id,
                    "name": "",
                }
            })
            .to_string(),
        )
    }

    fn function_args_delta(&mut self, index: usize, delta: &str, call_id: &str) -> String {
        sse_line(
            Some("response.function_call_arguments.delta"),
            &json!({
                "type": "response.function_call_arguments.delta",
                "sequence_number": self.next_seq(),
                "item_id": format!("fc_{call_id}"),
                "output_index": self.tool_output_index(index),
                "delta": delta,
            })
            .to_string(),
        )
    }

    fn close_funcs(&mut self) -> Vec<String> {
        if !self.in_func_block || self.funcs.is_empty() {
            return Vec::new();
        }
        let mut out = Vec::new();
        // 注意：不消费 funcs，completed 事件还需要读取；先收集再发事件，
        // 避免迭代 self.funcs 的同时调用 &mut self 方法。
        let funcs = self
            .funcs
            .iter()
            .map(|(index, state)| {
                (
                    *index,
                    state.call_id.clone(),
                    state.name.clone(),
                    if state.args.is_empty() {
                        "{}".to_owned()
                    } else {
                        state.args.clone()
                    },
                )
            })
            .collect::<Vec<_>>();
        for (index, call_id, name, args) in funcs {
            let output_index = self.tool_output_index(index);
            out.push(sse_line(
                Some("response.function_call_arguments.done"),
                &json!({
                    "type": "response.function_call_arguments.done",
                    "sequence_number": self.next_seq(),
                    "item_id": format!("fc_{call_id}"),
                    "output_index": output_index,
                    "arguments": args,
                })
                .to_string(),
            ));
            out.push(sse_line(
                Some("response.output_item.done"),
                &json!({
                    "type": "response.output_item.done",
                    "sequence_number": self.next_seq(),
                    "output_index": output_index,
                    "item": {
                        "id": format!("fc_{call_id}"),
                        "type": "function_call",
                        "status": "completed",
                        "arguments": args,
                        "call_id": call_id,
                        "name": name,
                    }
                })
                .to_string(),
            ));
        }
        self.in_func_block = false;
        out
    }

    fn completed_event(&mut self) -> String {
        let mut response = json!({
            "type": "response.completed",
            "sequence_number": self.next_seq(),
            "response": {
                "id": self.response_id,
                "object": "response",
                "created_at": self.created_at,
                "status": "completed",
                "background": false,
                "error": null,
                "output": [],
                "usage": {
                    "input_tokens": self.input_tokens,
                    "input_tokens_details": {"cached_tokens": self.cached_tokens},
                    "output_tokens": self.output_tokens,
                    "output_tokens_details": {},
                    "total_tokens": if self.total_tokens > 0 { self.total_tokens } else { self.input_tokens + self.output_tokens },
                },
            }
        });
        // 回显客户端请求字段
        for key in [
            "instructions",
            "max_output_tokens",
            "parallel_tool_calls",
            "previous_response_id",
            "reasoning",
            "temperature",
            "tool_choice",
            "tools",
            "top_p",
            "metadata",
        ] {
            if let Some(value) = self.original_request.get(key) {
                response["response"][key] = value.clone();
            }
        }
        let mut outputs = Vec::new();
        if !self.reasoning_buf.is_empty() || self.reasoning_part_added {
            outputs.push(json!({
                "id": self.reasoning_item_id.clone(),
                "type": "reasoning",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": self.reasoning_buf.clone()}],
            }));
        }
        if !self.text_buf.is_empty() {
            outputs.push(json!({
                "id": self.current_msg_id.clone(),
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "annotations": [], "logprobs": [], "text": self.text_buf.clone()}],
            }));
        }
        let funcs = self
            .funcs
            .iter()
            .map(|(index, state)| {
                (
                    *index,
                    state.call_id.clone(),
                    state.name.clone(),
                    if state.args.is_empty() {
                        "{}".to_owned()
                    } else {
                        state.args.clone()
                    },
                )
            })
            .collect::<Vec<_>>();
        for (_, call_id, name, args) in funcs {
            outputs.push(json!({
                "id": format!("fc_{call_id}"),
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": args,
            }));
        }
        if !outputs.is_empty() {
            response["response"]["output"] = Value::Array(outputs);
        }
        let reasoning_tokens = if self.reasoning_tokens > 0 {
            self.reasoning_tokens
        } else if self.reasoning_buf.is_empty() {
            0
        } else {
            (self.reasoning_buf.len() / 4) as u64
        };
        if reasoning_tokens > 0 {
            response["response"]["usage"]["output_tokens_details"]["reasoning_tokens"] =
                Value::from(reasoning_tokens);
        }
        sse_line(Some("response.completed"), &response.to_string())
    }
}

// =====================================================================
// 流式转换：Responses SSE -> Chat SSE
// =====================================================================

/// Responses SSE -> Chat SSE 增量转换器。
pub struct ResponsesToChatStreamer {
    model: String,
    chunk_id: String,
    role_sent: bool,
    done_sent: bool,
    current_tool_index: usize,
    current_tool_call_id: String,
    current_tool_name: String,
    current_tool_seq: usize,
    input_tokens: u64,
    output_tokens: u64,
    has_tool_call: bool,
}

impl ResponsesToChatStreamer {
    pub fn new(model: String) -> Self {
        Self {
            model,
            chunk_id: format!("chatcmpl-resp-{}", now_nanos()),
            role_sent: false,
            done_sent: false,
            current_tool_index: 0,
            current_tool_call_id: String::new(),
            current_tool_name: String::new(),
            current_tool_seq: 0,
            input_tokens: 0,
            output_tokens: 0,
            has_tool_call: false,
        }
    }

    /// 处理一个 Responses SSE 事件，返回 Chat chunk SSE 行。
    pub fn feed(&mut self, event: &SseEvent) -> Vec<String> {
        if event.is_done() {
            return Vec::new();
        }
        let Some(event_type) = event.event_type() else {
            return Vec::new();
        };
        let Ok(data) = serde_json::from_str::<Value>(&event.data) else {
            return Vec::new();
        };
        let mut out = Vec::new();
        match event_type.as_str() {
            "response.output_text.delta" => {
                if !self.role_sent {
                    self.role_sent = true;
                    out.push(self.chat_chunk(json!({"role": "assistant"}), None, None));
                }
                if let Some(text) = data.get("delta").and_then(Value::as_str)
                    && !text.is_empty()
                {
                    out.push(self.chat_chunk(json!({"content": text}), None, None));
                }
            }
            "response.reasoning_summary_text.delta" => {
                if let Some(text) = data.get("text").and_then(Value::as_str)
                    && !text.is_empty()
                {
                    out.push(self.chat_chunk(json!({"reasoning_content": text}), None, None));
                }
            }
            "response.output_item.added" => {
                let Some(item) = data.get("item") else {
                    return out;
                };
                if item.get("type").and_then(Value::as_str) == Some("function_call") {
                    self.has_tool_call = true;
                    self.current_tool_call_id = item
                        .get("call_id")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_owned();
                    self.current_tool_name = item
                        .get("name")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_owned();
                    self.current_tool_index = self.current_tool_seq;
                    self.current_tool_seq += 1;
                    out.push(self.chat_chunk(
                        json!({
                            "tool_calls": [{
                                "index": self.current_tool_index,
                                "id": self.current_tool_call_id,
                                "type": "function",
                                "function": {"name": self.current_tool_name, "arguments": ""},
                            }]
                        }),
                        None,
                        None,
                    ));
                }
            }
            "response.function_call_arguments.delta" => {
                if let Some(delta) = data.get("delta").and_then(Value::as_str)
                    && !delta.is_empty()
                {
                    out.push(self.chat_chunk(
                        json!({
                            "tool_calls": [{
                                "index": self.current_tool_index,
                                "function": {"arguments": delta},
                            }]
                        }),
                        None,
                        None,
                    ));
                }
            }
            "response.completed" => {
                if let Some(usage) = data.get("usage") {
                    self.input_tokens = usage
                        .get("input_tokens")
                        .and_then(Value::as_u64)
                        .unwrap_or(0);
                    self.output_tokens = usage
                        .get("output_tokens")
                        .and_then(Value::as_u64)
                        .unwrap_or(0);
                }
                let mut finish_reason = if self.has_tool_call {
                    "tool_calls"
                } else {
                    "stop"
                };
                if let Some(response) = data.get("response")
                    && response.get("status").and_then(Value::as_str) == Some("incomplete")
                {
                    finish_reason = "length";
                }
                let usage = if self.input_tokens > 0 || self.output_tokens > 0 {
                    Some(json!({
                        "prompt_tokens": self.input_tokens,
                        "completion_tokens": self.output_tokens,
                        "total_tokens": self.input_tokens + self.output_tokens,
                    }))
                } else {
                    None
                };
                out.push(self.chat_chunk(json!({}), Some(finish_reason), usage));
                out.push(crate::sse::done_line());
                self.done_sent = true;
            }
            _ => {}
        }
        out
    }

    /// 上游流结束兜底：尚未发送 [DONE] 时补发。
    pub fn finish(&mut self) -> Vec<String> {
        if self.done_sent {
            return Vec::new();
        }
        self.done_sent = true;
        vec![crate::sse::done_line()]
    }

    fn chat_chunk(
        &self,
        delta: Value,
        finish_reason: Option<&str>,
        usage: Option<Value>,
    ) -> String {
        let mut chunk = json!({
            "id": self.chunk_id,
            "object": "chat.completion.chunk",
            "created": unix_seconds(),
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        });
        // OpenAI Chat 流式约定：usage 位于 chunk 顶层（不在 choices 内）。
        if let Some(usage) = usage {
            chunk["usage"] = usage;
        }
        sse_line(None, &chunk.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sse::SseParser;

    #[test]
    fn responses_request_converts_to_chat() {
        let body = json!({
            "model": "ashare-advisor",
            "instructions": "你是助手",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "你好"}]},
                {"type": "function_call", "call_id": "call_1", "name": "get_price", "arguments": "{\"code\":\"600690\"}"},
                {"type": "function_call_output", "call_id": "call_1", "output": "10.5"}
            ],
            "tools": [{"type": "function", "name": "get_price", "parameters": {"type": "object"}}],
            "reasoning": {"effort": "minimal"},
            "max_output_tokens": 100,
            "stream": true,
        });
        let chat = responses_to_chat_request(&body, "gpt-4.1-mini", true);
        assert_eq!(chat["model"], "gpt-4.1-mini");
        assert_eq!(chat["stream"], true);
        assert_eq!(chat["stream_options"]["include_usage"], true);
        assert_eq!(chat["max_tokens"], 100);
        assert_eq!(chat["reasoning_effort"], "low");
        let messages = chat["messages"].as_array().unwrap();
        assert_eq!(messages.len(), 4);
        assert_eq!(messages[0]["role"], "system");
        assert_eq!(messages[1]["role"], "user");
        assert_eq!(messages[1]["content"], "你好");
        assert_eq!(messages[2]["role"], "assistant");
        assert_eq!(messages[2]["tool_calls"][0]["id"], "call_1");
        assert_eq!(
            messages[2]["tool_calls"][0]["function"]["name"],
            "get_price"
        );
        assert_eq!(messages[3]["role"], "tool");
        assert_eq!(messages[3]["tool_call_id"], "call_1");
        assert_eq!(messages[3]["content"], "10.5");
        assert_eq!(chat["tools"][0]["function"]["name"], "get_price");
        assert_eq!(
            chat["tools"][0]["function"]["parameters"]["required"],
            json!([])
        );
    }

    #[test]
    fn chat_request_converts_to_responses() {
        let body = json!({
            "model": "x",
            "messages": [
                {"role": "system", "content": "规则"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "ok"}
            ],
            "max_tokens": 64,
            "reasoning_effort": "high",
            "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
        });
        let responses = chat_to_responses_request(&body, "deepseek-chat");
        assert_eq!(responses["model"], "deepseek-chat");
        assert_eq!(responses["instructions"], "规则");
        assert_eq!(responses["max_output_tokens"], 64);
        assert_eq!(responses["reasoning"]["effort"], "high");
        let input = responses["input"].as_array().unwrap();
        assert_eq!(input.len(), 4);
        assert_eq!(input[0]["type"], "message");
        assert_eq!(input[0]["content"][0]["type"], "input_text");
        assert_eq!(input[1]["type"], "message");
        assert_eq!(input[2]["type"], "function_call");
        assert_eq!(input[2]["call_id"], "c1");
        assert_eq!(input[3]["type"], "function_call_output");
        assert_eq!(responses["tools"][0]["name"], "f");
    }

    #[test]
    fn chat_response_converts_to_responses_nonstream() {
        let chat = json!({
            "id": "chatcmpl-123",
            "created": 100,
            "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "答案", "reasoning_content": "思考"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        });
        let original = json!({"model": "ashare-advisor", "instructions": "指令"});
        let responses = chat_to_responses_nonstream(&chat, &original);
        assert_eq!(responses["object"], "response");
        assert_eq!(responses["status"], "completed");
        assert_eq!(responses["model"], "ashare-advisor");
        assert_eq!(responses["instructions"], "指令");
        assert_eq!(responses["output_text"], "答案");
        assert_eq!(responses["output"][0]["type"], "reasoning");
        assert_eq!(responses["output"][1]["type"], "message");
        assert_eq!(responses["output"][1]["content"][0]["text"], "答案");
        assert_eq!(responses["usage"]["input_tokens"], 10);
        assert_eq!(responses["usage"]["output_tokens"], 5);
        assert_eq!(responses["usage"]["total_tokens"], 15);
        // "思考" = 6 字节 UTF-8，按 len/4 估算。
        assert_eq!(
            responses["usage"]["output_tokens_details"]["reasoning_tokens"],
            1
        );
    }

    #[test]
    fn responses_response_converts_to_chat_nonstream() {
        let responses = json!({
            "id": "resp_1",
            "created_at": 100,
            "status": "completed",
            "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "你好"}]},
                {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{\"a\":1}"}
            ],
            "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}
        });
        let chat = responses_to_chat_nonstream(&responses, "client-model");
        assert_eq!(chat["object"], "chat.completion");
        assert_eq!(chat["model"], "client-model");
        assert_eq!(chat["choices"][0]["message"]["content"], "你好");
        assert_eq!(chat["choices"][0]["finish_reason"], "tool_calls");
        assert_eq!(chat["choices"][0]["message"]["tool_calls"][0]["id"], "c1");
        assert_eq!(chat["usage"]["prompt_tokens"], 3);
        assert_eq!(chat["usage"]["total_tokens"], 7);
    }

    fn stream_chat(chunks: Vec<Value>) -> Vec<SseEvent> {
        let mut streamer = ChatToResponsesStreamer::new(json!({"model": "ashare-advisor"}));
        let mut wire = String::new();
        for chunk in chunks {
            for line in streamer.feed(&chunk) {
                wire.push_str(&line);
            }
        }
        for line in streamer.finish() {
            wire.push_str(&line);
        }
        let mut parser = SseParser::new();
        parser.parse(wire.as_bytes())
    }

    #[test]
    fn chat_stream_converts_to_responses_events() {
        let events = stream_chat(vec![
            json!({"id": "chatcmpl-9", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "你"}, "finish_reason": null}]}),
            json!({"id": "chatcmpl-9", "choices": [{"index": 0, "delta": {"content": "好"}, "finish_reason": null}]}),
            json!({"id": "chatcmpl-9", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            json!({"id": "chatcmpl-9", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}),
        ]);
        let types = events
            .iter()
            .map(|event| event.event_type().unwrap_or_default())
            .collect::<Vec<_>>();
        assert_eq!(types[0], "response.created");
        assert_eq!(types[1], "response.in_progress");
        assert_eq!(types[2], "response.output_item.added");
        assert_eq!(types[3], "response.content_part.added");
        assert_eq!(types[4], "response.output_text.delta");
        assert_eq!(types[5], "response.output_text.delta");
        assert_eq!(types[6], "response.output_text.done");
        assert_eq!(types[7], "response.content_part.done");
        assert_eq!(types[8], "response.output_item.done");
        assert_eq!(types[9], "response.completed");
        let completed: Value = serde_json::from_str(&events[9].data).unwrap();
        assert_eq!(completed["response"]["status"], "completed");
        assert_eq!(completed["response"]["usage"]["input_tokens"], 5);
        assert_eq!(
            completed["response"]["output"][0]["content"][0]["text"],
            "你好"
        );
        assert_eq!(completed["response"]["id"], "chatcmpl-9");
    }

    #[test]
    fn chat_stream_converts_reasoning_and_tool_calls() {
        let events = stream_chat(vec![
            json!({"id": "chatcmpl-1", "choices": [{"index": 0, "delta": {"reasoning_content": "想"}, "finish_reason": null}]}),
            json!({"id": "chatcmpl-1", "choices": [{"index": 0, "delta": {"reasoning_content": "考"}, "finish_reason": null}]}),
            json!({"id": "chatcmpl-1", "choices": [{"index": 0, "delta": {"content": "正文"}, "finish_reason": null}]}),
            json!({"id": "chatcmpl-1", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{\"a\":"}}]}, "finish_reason": null}]}),
            json!({"id": "chatcmpl-1", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}, "finish_reason": null}]}),
            json!({"id": "chatcmpl-1", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
        ]);
        let types = events
            .iter()
            .map(|event| event.event_type().unwrap_or_default())
            .collect::<Vec<_>>();
        assert!(types.contains(&"response.reasoning_summary_text.delta".to_owned()));
        assert!(types.contains(&"response.reasoning_summary_text.done".to_owned()));
        assert!(types.contains(&"response.output_text.delta".to_owned()));
        assert!(types.contains(&"response.function_call_arguments.delta".to_owned()));
        let completed_index = types
            .iter()
            .position(|t| t == "response.completed")
            .unwrap();
        let completed: Value = serde_json::from_str(&events[completed_index].data).unwrap();
        let output = completed["response"]["output"].as_array().unwrap();
        assert_eq!(output[0]["type"], "reasoning");
        assert_eq!(output[0]["summary"][0]["text"], "想考");
        assert_eq!(output[1]["type"], "message");
        assert_eq!(output[1]["content"][0]["text"], "正文");
        assert_eq!(output[2]["type"], "function_call");
        assert_eq!(output[2]["call_id"], "call_1");
        assert_eq!(output[2]["arguments"], "{\"a\":1}");
    }

    #[test]
    fn think_tags_split_to_reasoning() {
        let events = stream_chat(vec![
            json!({"id": "chatcmpl-2", "choices": [{"index": 0, "delta": {"content": "<think>内"}, "finish_reason": null}]}),
            json!({"id": "chatcmpl-2", "choices": [{"index": 0, "delta": {"content": "容</think>正文"}, "finish_reason": null}]}),
            json!({"id": "chatcmpl-2", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        ]);
        let completed = events.last().unwrap();
        let completed: Value = serde_json::from_str(&completed.data).unwrap();
        let output = completed["response"]["output"].as_array().unwrap();
        assert_eq!(output[0]["type"], "reasoning");
        assert_eq!(output[0]["summary"][0]["text"], "内容");
        assert_eq!(output[1]["content"][0]["text"], "正文");
    }

    #[test]
    fn responses_stream_converts_to_chat_chunks() {
        let mut streamer = ResponsesToChatStreamer::new("client-model".to_owned());
        let mut wire = String::new();
        let mut parser = SseParser::new();
        let mut feed = |streamer: &mut ResponsesToChatStreamer,
                        parser: &mut SseParser,
                        event: &str,
                        data: Value| {
            let sse = sse_line(Some(event), &data.to_string());
            for ev in parser.parse(sse.as_bytes()) {
                for line in streamer.feed(&ev) {
                    wire.push_str(&line);
                }
            }
        };
        feed(
            &mut streamer,
            &mut parser,
            "response.output_text.delta",
            json!({"type": "response.output_text.delta", "delta": "你"}),
        );
        feed(
            &mut streamer,
            &mut parser,
            "response.output_text.delta",
            json!({"type": "response.output_text.delta", "delta": "好"}),
        );
        feed(
            &mut streamer,
            &mut parser,
            "response.completed",
            json!({
                "type": "response.completed",
                "response": {"id": "resp_1", "status": "completed"},
                "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}
            }),
        );
        for line in streamer.finish() {
            wire.push_str(&line);
        }
        assert!(wire.contains("\"role\":\"assistant\""));
        assert!(wire.contains("\"content\":\"你\""));
        assert!(wire.contains("\"content\":\"好\""));
        assert!(wire.ends_with("data: [DONE]\n\n"));
        // 结构断言：finish_reason 位于 choices[0]，usage 位于 chunk 顶层。
        let mut parser = SseParser::new();
        let events = parser.parse(wire.as_bytes());
        let final_chunk = events
            .iter()
            .filter(|event| !event.is_done())
            .filter_map(|event| serde_json::from_str::<Value>(&event.data).ok())
            .find(|chunk| chunk["choices"][0]["finish_reason"].is_string())
            .expect("final chunk");
        assert_eq!(final_chunk["choices"][0]["finish_reason"], "stop");
        assert_eq!(final_chunk["choices"][0]["delta"], json!({}));
        assert_eq!(final_chunk["usage"]["prompt_tokens"], 3);
        assert_eq!(final_chunk["usage"]["total_tokens"], 7);
        assert!(final_chunk["choices"][0].get("usage").is_none());
    }
}
