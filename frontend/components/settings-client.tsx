"use client";

import { useEffect, useState } from "react";
import { Check, RotateCcw } from "lucide-react";
import {
  DEFAULT_STUDIO_SETTINGS,
  readStudioSettings,
  writeStudioSettings,
  type StudioSettings,
} from "@/lib/studio-settings";
import { SiteNav } from "@/components/site-nav";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

export function SettingsClient() {
  const [settings, setSettings] = useState<StudioSettings>(DEFAULT_STUDIO_SETTINGS);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setSettings(readStudioSettings()));
    return () => window.cancelAnimationFrame(frame);
  }, []);

  const update = <K extends keyof StudioSettings>(key: K, value: StudioSettings[K]) => {
    setSaved(false);
    setSettings((current) => ({ ...current, [key]: value }));
  };

  const save = () => {
    writeStudioSettings(settings);
    setSaved(true);
  };

  const reset = () => {
    setSettings(DEFAULT_STUDIO_SETTINGS);
    setSaved(false);
  };

  return (
    <main className="relative min-h-screen px-4 py-5 sm:px-6 lg:px-8">
      <div className="mx-auto max-w-3xl space-y-8 pb-12">
        <SiteNav />
        <div>
          <h1 className="text-3xl font-medium tracking-[-0.04em] text-foreground/80">Studio settings</h1>
          <p className="mt-2 text-sm text-muted">Configure Bonsai-27B once for the prompt optimizer and the local chat.</p>
        </div>
        <section className="space-y-5 rounded-[1.5rem] border border-border-strong bg-surface-raised p-5 shadow-[var(--panel-shadow)]">
          <label className="flex cursor-pointer items-center justify-between gap-4 rounded-xl border border-border-strong bg-surface-strong p-4">
            <span>
              <span className="block text-sm font-semibold">Enable prompt optimizer</span>
              <span className="mt-1 block text-xs text-muted">The switch in the generator uses these settings.</span>
            </span>
            <input
              type="checkbox"
              checked={settings.promptOptimizerEnabled}
              onChange={(event) => update("promptOptimizerEnabled", event.target.checked)}
              className="size-5 accent-[var(--accent)]"
            />
          </label>
          <label className="block space-y-2 text-sm font-medium">
            Local LLM endpoint
            <Input value={settings.llmUrl} onChange={(event) => update("llmUrl", event.target.value)} placeholder="http://127.0.0.1:8081/v1" />
            <span className="block text-xs font-normal text-muted">Only local HTTP endpoints ending in <code>/v1</code> are accepted.</span>
          </label>
          <label className="block space-y-2 text-sm font-medium">
            Model identifier
            <Input value={settings.model} onChange={(event) => update("model", event.target.value)} />
          </label>
          <div className="space-y-4 rounded-xl border border-border-strong bg-surface-strong p-4">
            <div>
              <p className="text-sm font-semibold">Vision-Modell für Bilder</p>
              <p className="mt-1 text-xs text-muted">Text bleibt auf dem MLX-Server. Bildanhänge gehen nur an den lokalen GGUF-/llama.cpp-Vision-Server.</p>
            </div>
            <label className="block space-y-2 text-sm font-medium">
              Vision endpoint
              <Input value={settings.visionLlmUrl} onChange={(event) => update("visionLlmUrl", event.target.value)} placeholder="http://127.0.0.1:8080/v1" />
            </label>
            <label className="block space-y-2 text-sm font-medium">
              Vision model identifier
              <Input value={settings.visionModel} onChange={(event) => update("visionModel", event.target.value)} />
            </label>
          </div>
          <div className="space-y-3 rounded-xl border border-border-strong bg-surface-strong p-4">
            <div>
              <p className="text-sm font-semibold">Webrecherche</p>
              <p className="mt-1 text-xs text-muted">Wählt den Suchanbieter für den Chat. Schlüssel bleiben als lokale Umgebungsvariablen beim Studio-Backend und werden nicht im Browser gespeichert.</p>
            </div>
            <label className="block space-y-2 text-sm font-medium">
              Suchanbieter
              <select value={settings.webSearchProvider} onChange={(event) => update("webSearchProvider", event.target.value as StudioSettings["webSearchProvider"])} className="flex h-10 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground">
                <option value="auto">Automatisch (Tavily → Brave → Fallback)</option>
                <option value="tavily">Tavily</option>
                <option value="brave">Brave Search</option>
                <option value="fallback">Öffentliche Fallback-Suche</option>
              </select>
            </label>
            <p className="text-xs text-muted"><code>TAVILY_API_KEY</code> aktiviert Tavily, <code>BRAVE_SEARCH_API_KEY</code> aktiviert Brave. Ohne Schlüssel bleibt die automatische Auswahl beim transparent gekennzeichneten Fallback.</p>
          </div>
          <label className="block space-y-2 text-sm font-medium">
            Optimizer instruction
            <Textarea className="min-h-[160px] rounded-xl text-sm leading-6" value={settings.systemPrompt} onChange={(event) => update("systemPrompt", event.target.value)} />
          </label>
          <div className="flex flex-wrap gap-2 pt-1">
            <Button type="button" onClick={save}><Check className="size-4" />Save settings</Button>
            <Button type="button" variant="outline" onClick={reset}><RotateCcw className="size-4" />Reset</Button>
            {saved ? <span className="self-center text-xs text-muted">Saved locally in this browser.</span> : null}
          </div>
        </section>
      </div>
    </main>
  );
}
