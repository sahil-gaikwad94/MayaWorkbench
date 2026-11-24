package main

import "strings"

func Tokenize(text string) []string { return strings.Fields(text) }
func Score(parts []string) int { return len(parts) }
func NewIndex() map[string]int { return map[string]int{} }
