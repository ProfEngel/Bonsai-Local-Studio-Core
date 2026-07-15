export type PromptValidation =
  | { ok: true }
  | { ok: false; reason: "empty" };

export function validatePrompt(prompt: string): PromptValidation {
  const trimmed = prompt.trim();
  if (trimmed.length === 0) return { ok: false, reason: "empty" };
  return { ok: true };
}
