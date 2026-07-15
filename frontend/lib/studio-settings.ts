export const STUDIO_SETTINGS_STORAGE_KEY = "bonsai-studio-settings-v1";

export type StudioSettings = {
  promptOptimizerEnabled: boolean;
  llmUrl: string;
  model: string;
  visionLlmUrl: string;
  visionModel: string;
  systemPrompt: string;
};

export const DEFAULT_STUDIO_SETTINGS: StudioSettings = {
  promptOptimizerEnabled: false,
  llmUrl: "http://127.0.0.1:8081/v1",
  model: "prism-ml/Bonsai-27B-mlx-1bit",
  visionLlmUrl: "http://127.0.0.1:8080/v1",
  visionModel: "Bonsai-27B-Q1_0.gguf",
  systemPrompt:
    "You improve prompts for a Flux2 Klein image model. Return exactly one English image prompt, one sentence, at most 60 words, with no repetition or explanation. Preserve the user's subject and intent; add useful visual details such as composition, lighting, materials and style.",
};

export function readStudioSettings(): StudioSettings {
  if (typeof window === "undefined") return DEFAULT_STUDIO_SETTINGS;
  try {
    const raw = window.localStorage.getItem(STUDIO_SETTINGS_STORAGE_KEY);
    if (!raw) return DEFAULT_STUDIO_SETTINGS;
    const parsed = JSON.parse(raw) as Partial<StudioSettings>;
    return { ...DEFAULT_STUDIO_SETTINGS, ...parsed };
  } catch {
    return DEFAULT_STUDIO_SETTINGS;
  }
}

export function writeStudioSettings(settings: StudioSettings) {
  window.localStorage.setItem(STUDIO_SETTINGS_STORAGE_KEY, JSON.stringify(settings));
}
