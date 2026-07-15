export const STUDIO_SETTINGS_STORAGE_KEY = "bonsai-studio-settings-v1";

export type StudioSettings = {
  promptOptimizerEnabled: boolean;
  visionMode: "bonsai2bit" | "custom";
  llmUrl: string;
  model: string;
  visionLlmUrl: string;
  visionModel: string;
  webSearchProvider: "auto" | "tavily" | "brave" | "fallback";
  chatSystemPrompt: string;
  systemPrompt: string;
};

export const DEFAULT_STUDIO_SETTINGS: StudioSettings = {
  promptOptimizerEnabled: false,
  visionMode: "bonsai2bit",
  llmUrl: "http://127.0.0.1:8081/v1",
  model: "Ternary-Bonsai-27B-mlx-2bit",
  visionLlmUrl: "http://127.0.0.1:8080/v1",
  visionModel: "Ternary-Bonsai-27B-mlx-2bit",
  webSearchProvider: "auto",
  chatSystemPrompt:
    "Antworte kurz und direkt: normalerweise ein bis drei Sätze oder höchstens vier kurze Stichpunkte. Beginne sofort mit der Antwort. Keine Wiederholung der Frage, keine Selbstbeschreibung, keine Prozess- oder Tool-Erklärung und keine langen Standard-Hinweise. Wenn ein Entwurf, eine Analyse oder eine strukturierte Liste verlangt wird, liefere nur die dafür notwendigen Inhalte.",
  systemPrompt:
    "You improve prompts for a Flux2 Klein image model. Return exactly one English image prompt, one sentence, at most 60 words, with no repetition or explanation. Preserve the user's subject and intent; add useful visual details such as composition, lighting, materials and style.",
};

export function readStudioSettings(): StudioSettings {
  if (typeof window === "undefined") return DEFAULT_STUDIO_SETTINGS;
  try {
    const raw = window.localStorage.getItem(STUDIO_SETTINGS_STORAGE_KEY);
    if (!raw) return DEFAULT_STUDIO_SETTINGS;
    const parsed = JSON.parse(raw) as Partial<StudioSettings>;
    const settings = { ...DEFAULT_STUDIO_SETTINGS, ...parsed };
    if (!parsed.visionMode) {
      settings.visionMode = settings.model.includes("Ternary-Bonsai-27B-mlx-2bit") ? "bonsai2bit" : "custom";
    }
    return settings;
  } catch {
    return DEFAULT_STUDIO_SETTINGS;
  }
}

export function writeStudioSettings(settings: StudioSettings) {
  window.localStorage.setItem(STUDIO_SETTINGS_STORAGE_KEY, JSON.stringify(settings));
}

export async function readPersistentStudioSettings(): Promise<StudioSettings | null> {
  try {
    const response = await fetch("/api/studio-settings", { cache: "no-store" });
    const payload = await response.json() as { settings?: StudioSettings | null };
    if (!response.ok) return null;
    if (payload.settings) {
      writeStudioSettings(payload.settings);
      return payload.settings;
    }
    // First use after an update: promote the existing browser settings to the
    // durable local config automatically, without ever including search keys.
    const browserSettings = readStudioSettings();
    const migration = await fetch("/api/studio-settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(browserSettings),
    });
    const migrated = await migration.json() as { settings?: StudioSettings };
    if (!migration.ok || !migrated.settings) return null;
    writeStudioSettings(migrated.settings);
    return migrated.settings;
  } catch {
    return null;
  }
}
