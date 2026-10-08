//! A small JSON value with a writer and a parser: enough for `agent-ui dump`
//! and for reading `wezterm cli list --format json`, without serde.

use std::fmt::Write as _;

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Value>),
    /// Keys in insertion order (the dump stays readable and stable).
    Obj(Vec<(String, Value)>),
}

impl Value {
    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Obj(kv) => kv.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        if let Value::Str(s) = self {
            Some(s)
        } else {
            None
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        if let Value::Num(n) = self {
            Some(*n)
        } else {
            None
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        if let Value::Bool(b) = self {
            Some(*b)
        } else {
            None
        }
    }

    pub fn as_array(&self) -> Option<&[Value]> {
        if let Value::Arr(a) = self {
            Some(a)
        } else {
            None
        }
    }

    /// A scalar as text the way Python's `str(p.get(k))` would compare ids:
    /// numbers without a fraction print as integers.
    pub fn scalar_string(&self) -> Option<String> {
        match self {
            Value::Str(s) => Some(s.clone()),
            Value::Num(n) if n.fract() == 0.0 && n.abs() < 1e15 => Some(format!("{}", *n as i64)),
            Value::Num(n) => Some(n.to_string()),
            Value::Bool(b) => Some(if *b { "True".into() } else { "False".into() }),
            _ => None,
        }
    }

    /// Compact JSON.
    pub fn to_json(&self) -> String {
        let mut s = String::new();
        self.write(&mut s, None, 0);
        s
    }

    /// Indented JSON (2 spaces).
    pub fn to_json_pretty(&self) -> String {
        let mut s = String::new();
        self.write(&mut s, Some(2), 0);
        s
    }

    fn write(&self, out: &mut String, indent: Option<usize>, depth: usize) {
        let nl = |out: &mut String, d: usize| {
            if let Some(i) = indent {
                out.push('\n');
                out.push_str(&" ".repeat(i * d));
            }
        };
        match self {
            Value::Null => out.push_str("null"),
            Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Value::Num(n) => {
                if !n.is_finite() {
                    out.push_str("null");
                } else if n.fract() == 0.0 && n.abs() < 1e15 {
                    let _ = write!(out, "{}", *n as i64);
                } else {
                    let _ = write!(out, "{n}");
                }
            }
            Value::Str(s) => write_str(out, s),
            Value::Arr(a) => {
                if a.is_empty() {
                    out.push_str("[]");
                    return;
                }
                out.push('[');
                for (i, v) in a.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    nl(out, depth + 1);
                    v.write(out, indent, depth + 1);
                }
                nl(out, depth);
                out.push(']');
            }
            Value::Obj(kv) => {
                if kv.is_empty() {
                    out.push_str("{}");
                    return;
                }
                out.push('{');
                for (i, (k, v)) in kv.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    nl(out, depth + 1);
                    write_str(out, k);
                    out.push(':');
                    if indent.is_some() {
                        out.push(' ');
                    }
                    v.write(out, indent, depth + 1);
                }
                nl(out, depth);
                out.push('}');
            }
        }
    }
}

fn write_str(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 || c == '\u{7f}' || ('\u{80}'..='\u{9f}').contains(&c) => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

impl From<&str> for Value {
    fn from(s: &str) -> Value {
        Value::Str(s.to_string())
    }
}
impl From<String> for Value {
    fn from(s: String) -> Value {
        Value::Str(s)
    }
}
impl From<&String> for Value {
    fn from(s: &String) -> Value {
        Value::Str(s.clone())
    }
}
impl From<bool> for Value {
    fn from(b: bool) -> Value {
        Value::Bool(b)
    }
}
macro_rules! num_from {
    ($($t:ty),*) => {$(impl From<$t> for Value { fn from(n: $t) -> Value { Value::Num(n as f64) } })*};
}
num_from!(i32, i64, u8, u32, u64, usize, f64);
impl<T: Into<Value>> From<Option<T>> for Value {
    fn from(o: Option<T>) -> Value {
        o.map(Into::into).unwrap_or(Value::Null)
    }
}
impl<T: Into<Value>> From<Vec<T>> for Value {
    fn from(v: Vec<T>) -> Value {
        Value::Arr(v.into_iter().map(Into::into).collect())
    }
}

/// Build an object: `obj![("a", 1), ("b", "x")]`.
#[macro_export]
macro_rules! obj {
    ($(($k:expr, $v:expr)),* $(,)?) => {
        $crate::json::Value::Obj(vec![$(($k.to_string(), $crate::json::Value::from($v))),*])
    };
}

/// Parse JSON text. Errors carry the byte offset.
pub fn parse(text: &str) -> Result<Value, String> {
    let mut p = Parser { s: text.as_bytes(), i: 0 };
    p.ws();
    let v = p.value(0)?;
    p.ws();
    if p.i != p.s.len() {
        return Err(format!("trailing data at {}", p.i));
    }
    Ok(v)
}

struct Parser<'a> {
    s: &'a [u8],
    i: usize,
}

impl Parser<'_> {
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn err<T>(&self, what: &str) -> Result<T, String> {
        Err(format!("{what} at {}", self.i))
    }

    fn lit(&mut self, word: &str, v: Value) -> Result<Value, String> {
        if self.s[self.i..].starts_with(word.as_bytes()) {
            self.i += word.len();
            Ok(v)
        } else {
            self.err("bad literal")
        }
    }

    fn value(&mut self, depth: usize) -> Result<Value, String> {
        if depth > 128 {
            return self.err("too deep");
        }
        match self.s.get(self.i) {
            None => self.err("unexpected end"),
            Some(b'n') => self.lit("null", Value::Null),
            Some(b't') => self.lit("true", Value::Bool(true)),
            Some(b'f') => self.lit("false", Value::Bool(false)),
            Some(b'"') => self.string().map(Value::Str),
            Some(b'[') => {
                self.i += 1;
                let mut a = Vec::new();
                self.ws();
                if self.s.get(self.i) == Some(&b']') {
                    self.i += 1;
                    return Ok(Value::Arr(a));
                }
                loop {
                    self.ws();
                    a.push(self.value(depth + 1)?);
                    self.ws();
                    match self.s.get(self.i) {
                        Some(b',') => self.i += 1,
                        Some(b']') => {
                            self.i += 1;
                            return Ok(Value::Arr(a));
                        }
                        _ => return self.err("expected , or ]"),
                    }
                }
            }
            Some(b'{') => {
                self.i += 1;
                let mut kv = Vec::new();
                self.ws();
                if self.s.get(self.i) == Some(&b'}') {
                    self.i += 1;
                    return Ok(Value::Obj(kv));
                }
                loop {
                    self.ws();
                    if self.s.get(self.i) != Some(&b'"') {
                        return self.err("expected key");
                    }
                    let k = self.string()?;
                    self.ws();
                    if self.s.get(self.i) != Some(&b':') {
                        return self.err("expected :");
                    }
                    self.i += 1;
                    self.ws();
                    kv.push((k, self.value(depth + 1)?));
                    self.ws();
                    match self.s.get(self.i) {
                        Some(b',') => self.i += 1,
                        Some(b'}') => {
                            self.i += 1;
                            return Ok(Value::Obj(kv));
                        }
                        _ => return self.err("expected , or }"),
                    }
                }
            }
            Some(_) => self.number(),
        }
    }

    fn number(&mut self) -> Result<Value, String> {
        let start = self.i;
        while self.i < self.s.len() && matches!(self.s[self.i], b'-' | b'+' | b'.' | b'e' | b'E' | b'0'..=b'9') {
            self.i += 1;
        }
        let t = std::str::from_utf8(&self.s[start..self.i]).unwrap_or("");
        t.parse::<f64>().map(Value::Num).or_else(|_| self.err("bad number"))
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let h = self.s.get(self.i..self.i + 4).and_then(|b| std::str::from_utf8(b).ok());
        let n = h.and_then(|h| u32::from_str_radix(h, 16).ok());
        match n {
            Some(n) => {
                self.i += 4;
                Ok(n)
            }
            None => self.err("bad \\u escape"),
        }
    }

    fn string(&mut self) -> Result<String, String> {
        self.i += 1; // opening quote
        let mut out = Vec::new();
        loop {
            match self.s.get(self.i) {
                None => return self.err("unterminated string"),
                Some(b'"') => {
                    self.i += 1;
                    return Ok(String::from_utf8_lossy(&out).into_owned());
                }
                Some(b'\\') => {
                    self.i += 1;
                    let c = *self.s.get(self.i).ok_or("bad escape")?;
                    self.i += 1;
                    let ch = match c {
                        b'"' => '"',
                        b'\\' => '\\',
                        b'/' => '/',
                        b'b' => '\u{8}',
                        b'f' => '\u{c}',
                        b'n' => '\n',
                        b'r' => '\r',
                        b't' => '\t',
                        b'u' => {
                            let hi = self.hex4()?;
                            let cp = if (0xD800..0xDC00).contains(&hi) && self.s[self.i..].starts_with(b"\\u") {
                                self.i += 2;
                                let lo = self.hex4()?;
                                0x10000 + ((hi - 0xD800) << 10) + (lo.wrapping_sub(0xDC00) & 0x3FF)
                            } else {
                                hi
                            };
                            char::from_u32(cp).unwrap_or('\u{FFFD}')
                        }
                        _ => return self.err("bad escape"),
                    };
                    let mut b = [0u8; 4];
                    out.extend_from_slice(ch.encode_utf8(&mut b).as_bytes());
                }
                Some(&b) => {
                    out.push(b);
                    self.i += 1;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip() {
        let v = obj![("a", 1), ("b", "x\"y\n\u{1f}"), ("c", vec![true, false]), ("d", Value::Null), ("e", 1.5)];
        let s = v.to_json();
        assert_eq!(s, r#"{"a":1,"b":"x\"y\n\u001f","c":[true,false],"d":null,"e":1.5}"#);
        assert_eq!(parse(&s).unwrap(), v);
        assert_eq!(parse(&v.to_json_pretty()).unwrap(), v);
    }

    #[test]
    fn wezterm_like() {
        let t = r#"[{"window_id":0,"tab_id":3,"pane_id":7,"size":{"rows":40,"cols":120},"title":"🫠 🫠","tty_name":"/dev/ttys004","is_active":true}]"#;
        let v = parse(t).unwrap();
        let p = &v.as_array().unwrap()[0];
        assert_eq!(p.get("pane_id").unwrap().scalar_string().unwrap(), "7");
        assert_eq!(p.get("tty_name").unwrap().as_str(), Some("/dev/ttys004"));
        assert_eq!(p.get("title").unwrap().as_str(), Some("🫠 🫠"));
        assert!(parse("[1,").is_err());
        assert!(parse("{\"a\" 1}").is_err());
        assert!(parse("[] x").is_err());
    }
}
