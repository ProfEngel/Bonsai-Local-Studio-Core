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

const BONSAI_2BIT_SETTINGS = {
  visionMode: "bonsai2bit" as const,
  llmUrl: "http://127.0.0.1:8081/v1",
  model: "Ternary-Bonsai-27B-mlx-2bit",
  visionLlmUrl: "http://127.0.0.1:8080/v1",
  visionModel: "Ternary-Bonsai-27B-mlx-2bit",
};

export function SettingsClient() {
  const [settings, setSettings] = useState<StudioSettings>(DEFAULT_STUDIO_SETTINGS);
  const [saved, setSaved] = useState(false);
  const [tavilyKey, setTavilyKey] = useState("");
  const [braveKey, setBraveKey] = useState("");
  const [searchStatus, setSearchStatus] = useState({ tavilyConfigured: false, braveConfigured: false });
  const [searchSaving, setSearchSaving] = useState(false);
  const [searchMessage, setSearchMessage] = useState("");

  useEffect(() => {
    let active = true;
    const browserSettings = readStudioSettings();
    void fetch("/api/studio-settings", { cache: "no-store" })
      .then(async (response) => response.ok ? response.json() : Promise.reject(new Error("status")))
      .then(async (payload: { settings?: StudioSettings | null }) => {
        if (!active) return;
        if (payload.settings) {
          writeStudioSettings(payload.settings);
          setSettings(payload.settings);
          return;
        }
        // One-time migration of earlier browser-only settings. Search keys stay
        // in their separate private file and are never read into the browser.
        const migration = await fetch("/api/studio-settings", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(browserSettings),
        });
        const migrated = await migration.json() as { settings?: StudioSettings };
        if (active && migration.ok && migrated.settings) {
          writeStudioSettings(migrated.settings);
          setSettings(migrated.settings);
        }
      })
      .catch(() => active && setSettings(browserSettings));
    return () => { active = false; };
  }, []);

  useEffect(() => {
    void fetch("/api/web-search/config", { cache: "no-store" })
      .then(async (response) => response.ok ? response.json() : Promise.reject(new Error("status")))
      .then((status) => setSearchStatus({ tavilyConfigured: Boolean(status.tavilyConfigured), braveConfigured: Boolean(status.braveConfigured) }))
      .catch(() => setSearchMessage("Suchanbieter konnten nicht geprüft werden."));
  }, []);

  const update = <K extends keyof StudioSettings>(key: K, value: StudioSettings[K]) => {
    setSaved(false);
    setSettings((current) => ({ ...current, [key]: value }));
  };

  const selectVisionMode = (visionMode: StudioSettings["visionMode"]) => {
    setSaved(false);
    setSettings((current) => visionMode === "bonsai2bit"
      ? { ...current, ...BONSAI_2BIT_SETTINGS }
      : { ...current, visionMode: "custom" });
  };

  const save = async () => {
    writeStudioSettings(settings);
    try {
      const response = await fetch("/api/studio-settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
      });
      const payload = await response.json() as { settings?: StudioSettings; detail?: string };
      if (!response.ok || !payload.settings) throw new Error(payload.detail || "Speichern fehlgeschlagen.");
      writeStudioSettings(payload.settings);
      setSettings(payload.settings);
      setSaved(true);
      if (tavilyKey.trim() || braveKey.trim()) await saveSearchKeys();
    } catch (error) {
      setSearchMessage(error instanceof Error ? error.message : "Studio-Einstellungen konnten nicht gespeichert werden.");
    }
  };

  const reset = () => {
    setSettings(DEFAULT_STUDIO_SETTINGS);
    setSaved(false);
  };

  const saveSearchKeys = async () => {
    if (!tavilyKey.trim() && !braveKey.trim()) {
      setSearchMessage("Gib mindestens einen Schlüssel ein.");
      return;
    }
    setSearchSaving(true);
    setSearchMessage("");
    try {
      const response = await fetch("/api/web-search/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tavily_api_key: tavilyKey, brave_api_key: braveKey }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Speichern fehlgeschlagen.");
      setSearchStatus({ tavilyConfigured: Boolean(body.tavilyConfigured), braveConfigured: Boolean(body.braveConfigured) });
      setTavilyKey("");
      setBraveKey("");
      setSearchMessage("Lokal mit privaten Dateirechten gespeichert.");
    } catch (error) {
      setSearchMessage(error instanceof Error ? error.message : "Speichern fehlgeschlagen.");
    } finally {
      setSearchSaving(false);
    }
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
          <div className="space-y-3 rounded-xl border border-border-strong bg-surface-strong p-4">
            <label className="block space-y-2 text-sm font-medium">
              Lokales Modell
              <select value={settings.visionMode} onChange={(event) => selectVisionMode(event.target.value as StudioSettings["visionMode"])} className="flex h-10 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground">
                <option value="bonsai2bit">Bonsai‑27B 2Bit mit Vision (empfohlen)</option>
                <option value="custom">Eigenes Modell / eigener Vision-Server</option>
              </select>
            </label>
            {settings.visionMode === "bonsai2bit" ? <p className="text-xs text-muted">Ein gemeinsames Modell: Text über <code>127.0.0.1:8081</code>, Bilder über <code>127.0.0.1:8080</code>. Die Vision-Erweiterung ist bereits enthalten.</p> : <div className="space-y-4">
              <label className="block space-y-2 text-sm font-medium">
                Local LLM endpoint
                <Input value={settings.llmUrl} onChange={(event) => update("llmUrl", event.target.value)} placeholder="http://127.0.0.1:8081/v1" />
                <span className="block text-xs font-normal text-muted">Only local HTTP endpoints ending in <code>/v1</code> are accepted.</span>
              </label>
              <label className="block space-y-2 text-sm font-medium">
                Model identifier
                <Input value={settings.model} onChange={(event) => update("model", event.target.value)} />
              </label>
              <label className="block space-y-2 text-sm font-medium">
                Vision endpoint
                <Input value={settings.visionLlmUrl} onChange={(event) => update("visionLlmUrl", event.target.value)} placeholder="http://127.0.0.1:8080/v1" />
              </label>
              <label className="block space-y-2 text-sm font-medium">
                Vision model identifier
                <Input value={settings.visionModel} onChange={(event) => update("visionModel", event.target.value)} />
              </label>
            </div>}
          </div>
          <div className="space-y-3 rounded-xl border border-border-strong bg-surface-strong p-4">
            <div>
              <p className="text-sm font-semibold">Webrecherche</p>
              <p className="mt-1 text-xs text-muted">Wählt den Suchanbieter für den Chat. Schlüssel werden nur lokal im Studio-Backend abgelegt und nie wieder an den Browser zurückgegeben.</p>
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
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block space-y-2 text-sm font-medium">Tavily API-Schlüssel <span className="text-xs font-normal text-muted">{searchStatus.tavilyConfigured ? "konfiguriert" : "noch nicht konfiguriert"}</span><Input type="password" autoComplete="new-password" value={tavilyKey} onChange={(event) => setTavilyKey(event.target.value)} placeholder="tvly-…" /></label>
              <label className="block space-y-2 text-sm font-medium">Brave Search API-Schlüssel <span className="text-xs font-normal text-muted">{searchStatus.braveConfigured ? "konfiguriert" : "noch nicht konfiguriert"}</span><Input type="password" autoComplete="new-password" value={braveKey} onChange={(event) => setBraveKey(event.target.value)} placeholder="BSA…" /></label>
            </div>
            <div className="flex flex-wrap items-center gap-3"><Button type="button" variant="outline" size="sm" onClick={() => void saveSearchKeys()} disabled={searchSaving}>{searchSaving ? "Speichere …" : "Suchschlüssel lokal speichern"}</Button>{searchMessage ? <span className="text-xs text-muted">{searchMessage}</span> : null}</div>
            <p className="text-xs text-muted">Automatisch nutzt Tavily, dann Brave, dann den transparent markierten öffentlichen Fallback. Bei Tavily oder Brave wird nur der aktuelle Suchprompt an den gewählten Anbieter gesendet.</p>
          </div>
          <label className="block space-y-2 text-sm font-medium">
            Chat-Systemanweisung
            <Textarea className="min-h-[150px] rounded-xl text-sm leading-6" value={settings.chatSystemPrompt} onChange={(event) => update("chatSystemPrompt", event.target.value)} />
            <span className="block text-xs font-normal text-muted">Gilt für den allgemeinen Chat und zusätzlich für alle Agentenläufe. Die Antworten bleiben immer deutsch; Sicherheitsregeln der Agenten werden dadurch nicht geändert.</span>
          </label>
          <label className="block space-y-2 text-sm font-medium">
            Optimizer instruction
            <Textarea className="min-h-[160px] rounded-xl text-sm leading-6" value={settings.systemPrompt} onChange={(event) => update("systemPrompt", event.target.value)} />
          </label>
          <div className="flex flex-wrap gap-2 pt-1">
            <Button type="button" onClick={() => void save()}><Check className="size-4" />Alles lokal speichern</Button>
            <Button type="button" variant="outline" onClick={reset}><RotateCcw className="size-4" />Reset</Button>
            {saved ? <span className="self-center text-xs text-muted">Updatefest lokal gespeichert.</span> : null}
          </div>
        </section>
      </div>
    </main>
  );
}
