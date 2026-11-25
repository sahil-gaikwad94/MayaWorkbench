use std::collections::HashMap;

pub struct Index { inner: HashMap<String, usize> }

pub fn tokenize(text: &str) -> Vec<&str> { text.split_whitespace().collect() }
pub fn score(parts: &[&str]) -> usize { parts.len() }
