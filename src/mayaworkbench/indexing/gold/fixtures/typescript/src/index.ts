import * as fs from "fs";
import * as path from "path";

export interface Score { value: number; }

export function tokenize(text: string): string[] {
  return text.trim().split(/\s+/);
}

export function scoreAll(items: string[]): Score[] {
  return items.map(value => ({ value: tokenize(value).length }));
}
