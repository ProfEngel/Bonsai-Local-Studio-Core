import { describe, expect, it } from "vitest";
import { validatePrompt } from "@/lib/prompt-moderator";

describe("validatePrompt", () => {
  it("accepts neutral prompts", () => {
    const neutral = [
      "A serene Scandinavian woman in her 40s, soft window light",
      "A koi fish gliding through lily-pad covered water",
      "Cinematic landscape of a forest at dawn",
      "Abstract emerald ink swirling through clear water",
      "Hand-lettered storefront sign at golden hour",
    ];
    for (const prompt of neutral) {
      expect(validatePrompt(prompt)).toEqual({ ok: true });
    }
  });

  it("rejects empty / whitespace-only prompts", () => {
    expect(validatePrompt("")).toEqual({ ok: false, reason: "empty" });
    expect(validatePrompt("   \n\t  ")).toEqual({ ok: false, reason: "empty" });
  });

  it("does not filter prompt content", () => {
    const unrestricted = [
      "a gruesome horror creature in a dungeon",
      "an erotic fantasy book cover",
      "an adult dark-fantasy character portrait",
    ];
    for (const prompt of unrestricted) {
      expect(validatePrompt(prompt)).toEqual({ ok: true });
    }
  });
});
