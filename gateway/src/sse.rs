//! SSE（Server-Sent Events）解析与序列化工具。
//!
//! OpenAI Chat Completions 与 Responses API 的流式响应都是 SSE 格式。
//! 解析按字节进行，跨 chunk 的“半行”与未结束的事件（event:/data: 字段
//! 已到达但空行未到）都会保留在解析器状态中，因此可以安全地处理任意
//! 分片边界（包括 JSON 中的原始 UTF-8）。

#[derive(Clone, Debug)]
pub struct SseEvent {
    /// `event:` 字段；缺失时由 data 内的 `type` 字段推断。
    pub event: Option<String>,
    /// `data:` 字段（多行 data 按 SSE 规范以 \n 连接）。
    pub data: String,
}

impl SseEvent {
    /// data 是否为 `[DONE]` 结束标记。
    pub fn is_done(&self) -> bool {
        self.data == "[DONE]"
    }

    /// 事件类型：优先 `event:` 字段，其次 data JSON 的 `type` 字段。
    pub fn event_type(&self) -> Option<String> {
        if let Some(event) = self.event.as_deref()
            && !event.is_empty()
        {
            return Some(event.to_owned());
        }
        serde_json::from_str::<serde_json::Value>(&self.data)
            .ok()
            .and_then(|value| {
                value
                    .get("type")
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_owned)
            })
    }
}

/// 增量 SSE 解析器：跨调用保留未完成的行与事件字段。
#[derive(Default)]
pub struct SseParser {
    remainder: Vec<u8>,
    event_type: Option<String>,
    data_lines: Vec<String>,
}

impl SseParser {
    pub fn new() -> Self {
        Self::default()
    }

    /// 解析一段上游字节流，产出完整 SSE 事件。
    pub fn parse(&mut self, bytes: &[u8]) -> Vec<SseEvent> {
        self.remainder.extend_from_slice(bytes);
        let mut events = Vec::new();
        let mut start = 0;
        let mut index = 0;
        while index < self.remainder.len() {
            if self.remainder[index] != b'\n' {
                index += 1;
                continue;
            }
            let mut line = self.remainder[start..index].to_vec();
            if line.last() == Some(&b'\r') {
                line.pop();
            }
            self.process_line(&line, &mut events);
            start = index + 1;
            index += 1;
        }
        self.remainder.drain(..start);
        events
    }

    fn process_line(&mut self, line: &[u8], events: &mut Vec<SseEvent>) {
        if line.is_empty() {
            // 空行结束当前事件。
            let data = self.data_lines.join("\n");
            self.data_lines.clear();
            let event = self.event_type.take();
            if !data.is_empty() {
                events.push(SseEvent { event, data });
            }
            return;
        }
        if line.starts_with(b":") {
            return; // 注释行
        }
        if let Some(rest) = line.strip_prefix(b"event:") {
            self.event_type = Some(trim_ascii(rest));
        } else if let Some(rest) = line.strip_prefix(b"data:") {
            self.data_lines.push(trim_ascii(rest));
        }
        // id: / retry: 字段对 OpenAI 兼容流没有语义，忽略。
    }
}

fn trim_ascii(bytes: &[u8]) -> String {
    let trimmed = bytes
        .iter()
        .position(|byte| !byte.is_ascii_whitespace())
        .map(|start| &bytes[start..])
        .unwrap_or(&[]);
    String::from_utf8_lossy(trimmed).into_owned()
}

/// 序列化一个 SSE 事件（带 `event:` 字段时输出两行，否则仅 data 行）。
pub fn sse_line(event: Option<&str>, data: &str) -> String {
    match event {
        Some(event) if !event.is_empty() => format!("event: {event}\ndata: {data}\n\n"),
        _ => format!("data: {data}\n\n"),
    }
}

/// `data: [DONE]` 结束行（Chat Completions 流）。
pub fn done_line() -> String {
    "data: [DONE]\n\n".to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_across_chunk_boundaries() {
        let mut parser = SseParser::new();
        let first = parser.parse(b"data: {\"a\":1}\n\nevent: x\nda");
        assert_eq!(first.len(), 1);
        assert_eq!(first[0].data, "{\"a\":1}");
        assert!(first[0].event.is_none());

        let second = parser.parse(b"ta: {\"b\":2}\n\n");
        assert_eq!(second.len(), 1);
        assert_eq!(second[0].event.as_deref(), Some("x"));
        assert_eq!(second[0].data, "{\"b\":2}");

        let third = parser.parse(b"data: {\"c\":3}\n\ndata: {\"d\":4}\n\n");
        assert_eq!(third.len(), 2);
        assert_eq!(third[0].data, "{\"c\":3}");
        assert_eq!(third[1].data, "{\"d\":4}");
    }

    #[test]
    fn handles_data_colon_and_multiline() {
        let mut parser = SseParser::new();
        let events = parser.parse(b"data:first\ndata: second\n\n");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].data, "first\nsecond");
    }

    #[test]
    fn infers_event_type_from_json() {
        let mut parser = SseParser::new();
        let events = parser.parse(b"data: {\"type\":\"response.completed\"}\n\n");
        assert_eq!(
            events[0].event_type().as_deref(),
            Some("response.completed")
        );
    }

    #[test]
    fn serializes_events() {
        assert_eq!(sse_line(None, "{}"), "data: {}\n\n");
        assert_eq!(
            sse_line(Some("response.created"), "{}"),
            "event: response.created\ndata: {}\n\n"
        );
        assert_eq!(done_line(), "data: [DONE]\n\n");
    }
}
