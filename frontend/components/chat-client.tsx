"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import NextImage from "next/image";
import ReactMarkdown from "react-markdown";
import { FileText, Image as ImageIcon, LoaderCircle, MessageCircle, Paperclip, Plus, Search, Send, Trash2, X } from "lucide-react";
import { SiteNav } from "@/components/site-nav";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { readStudioSettings } from "@/lib/studio-settings";

const CHAT_HISTORY_KEY = "bonsai-chat-history-v1";
const MAX_FILE_BYTES = 8 * 1024 * 1024;
const MAX_ATTACHMENTS = 5;
const EXCERPT_LIMIT = 24_000;
const HISTORY_EXCERPT_LIMIT = 6_000;

type AttachmentKind = "text" | "pdf" | "image";
type Attachment = { id: string; name: string; kind: AttachmentKind; excerpt: string; note?: string; previewDataUrl?: string; dataUrl?: string };
type Source = { title: string; url: string; snippet: string };
type ChatAgentProfile = { id: string; name: string; description: string; webSearchDefault: boolean; systemPrompt: string };
type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: string;
  attachments?: Attachment[];
  sources?: Source[];
  runner?: string;
};
type Conversation = { id: string; title: string; createdAt: string; updatedAt: string; messages: ChatMessage[] };

const TEXT_EXTENSIONS = new Set(["txt", "md", "csv", "json", "html", "htm", "xml", "yaml", "yml", "py", "ts", "tsx", "js", "jsx", "css", "sql", "log"]);

function makeId() {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function clip(value: string, limit: number) {
  return value.length <= limit ? value : `${value.slice(0, limit)}\n\n[Auszug gekürzt]`;
}

function titleFor(value: string) {
  const clean = value.replace(/\s+/g, " ").trim();
  return clean ? clip(clean, 44) : "Neue Unterhaltung";
}

function createConversation(): Conversation {
  const now = new Date().toISOString();
  return { id: makeId(), title: "Neue Unterhaltung", createdAt: now, updatedAt: now, messages: [] };
}

function parseError(payload: unknown, fallback: string) {
  if (payload && typeof payload === "object" && "detail" in payload && typeof payload.detail === "string") return payload.detail;
  return fallback;
}

function isPdf(file: File) {
  return file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
}

function isText(file: File) {
  const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
  return file.type.startsWith("text/") || TEXT_EXTENSIONS.has(extension);
}

async function toBase64(file: File) {
  let buffer: ArrayBuffer;
  try {
    buffer = await file.arrayBuffer();
  } catch {
    throw new Error(`${file.name} konnte nicht gelesen werden. Wenn die Datei in iCloud Drive liegt, bitte erst vollständig auf den Mac laden und erneut auswählen.`);
  }
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

async function createImagePreview(file: File) {
  const objectUrl = URL.createObjectURL(file);
  try {
    const image = await new Promise<HTMLImageElement>((resolve, reject) => {
      const element = new Image();
      element.onload = () => resolve(element);
      element.onerror = () => reject(new Error("Die Bildvorschau konnte nicht erstellt werden."));
      element.src = objectUrl;
    });
    const longestSide = 112;
    const scale = Math.min(1, longestSide / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    canvas.getContext("2d")?.drawImage(image, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/jpeg", 0.72);
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

async function readImageDataUrl(file: File) {
  if (file.size > 6 * 1024 * 1024) throw new Error(`„${file.name}“ ist zu groß für die lokale Bildanalyse (maximal 6 MB). Bitte verkleinere oder beschneide das Bild.`);
  const encoded = await toBase64(file);
  return `data:${file.type || "image/jpeg"};base64,${encoded}`;
}

function historyAttachment(attachment: Attachment): Attachment {
  const stored = { ...attachment };
  delete stored.dataUrl;
  return { ...stored, excerpt: clip(stored.excerpt, HISTORY_EXCERPT_LIMIT) };
}

export function ChatClient() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [webSearch, setWebSearch] = useState(false);
  const [agentProfiles, setAgentProfiles] = useState<ChatAgentProfile[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [isSending, setIsSending] = useState(false);
  const [isReadingFiles, setIsReadingFiles] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      try {
        const stored = window.localStorage.getItem(CHAT_HISTORY_KEY);
        const parsed = stored ? JSON.parse(stored) as Conversation[] : [];
        const valid = Array.isArray(parsed) ? parsed.filter((item) => item && Array.isArray(item.messages)) : [];
        const initial = valid.length > 0 ? valid : [createConversation()];
        setConversations(initial);
        setActiveId(initial[0].id);
      } catch {
        const initial = createConversation();
        setConversations([initial]);
        setActiveId(initial.id);
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    void fetch("/api/chat", { cache: "no-store" })
      .then((response) => response.ok ? response.json() : { agents: [] })
      .then((payload: { agents?: ChatAgentProfile[] }) => setAgentProfiles(Array.isArray(payload.agents) ? payload.agents : []))
      .catch(() => setAgentProfiles([]));
  }, []);

  useEffect(() => {
    if (conversations.length > 0) window.localStorage.setItem(CHAT_HISTORY_KEY, JSON.stringify(conversations));
  }, [conversations]);

  const activeConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === activeId) ?? conversations[0] ?? null,
    [activeId, conversations],
  );
  const selectedAgent = useMemo(
    () => agentProfiles.find((agent) => agent.id === selectedAgentId) ?? null,
    [agentProfiles, selectedAgentId],
  );

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [activeConversation?.messages.length, isSending]);

  const startConversation = useCallback(() => {
    const conversation = createConversation();
    setConversations((current) => [conversation, ...current]);
    setActiveId(conversation.id);
    setDraft("");
    setAttachments([]);
    setError(null);
  }, []);

  const deleteConversation = useCallback((id: string) => {
    setConversations((current) => {
      const remaining = current.filter((conversation) => conversation.id !== id);
      if (remaining.length > 0) {
        if (id === activeId) setActiveId(remaining[0].id);
        return remaining;
      }
      const replacement = createConversation();
      setActiveId(replacement.id);
      return [replacement];
    });
  }, [activeId]);

  const readFile = useCallback(async (file: File): Promise<Attachment> => {
    if (file.size > MAX_FILE_BYTES) throw new Error(`${file.name} ist größer als 8 MB.`);
    const id = makeId();
    if (file.type.startsWith("image/")) {
      const dataUrl = await readImageDataUrl(file);
      let previewDataUrl = dataUrl;
      try {
        previewDataUrl = await createImagePreview(file);
      } catch {
        // A browser may not decode every locally supported camera format into a
        // canvas. The original local data URL is still a valid thumbnail source.
      }
      return {
        id,
        name: file.name,
        kind: "image",
        excerpt: "Bildanhang für lokale Vision-Analyse.",
        note: "Lokales Vision-Modell wird verwendet.",
        previewDataUrl,
        dataUrl,
      };
    }
    if (isPdf(file)) {
      const response = await fetch("/api/chat/extract-pdf", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, data_base64: await toBase64(file) }),
      });
      const payload = await response.json().catch(() => null) as { text?: string; pages?: number; detail?: string } | null;
      if (!response.ok || !payload?.text) throw new Error(parseError(payload, `${file.name} konnte nicht gelesen werden.`));
      return { id, name: file.name, kind: "pdf", excerpt: clip(payload.text, EXCERPT_LIMIT), note: `${payload.pages ?? "?"} PDF-Seiten lokal ausgelesen.` };
    }
    if (isText(file)) {
      const text = (await file.text()).trim();
      if (!text) throw new Error(`${file.name} enthält keinen lesbaren Text.`);
      return { id, name: file.name, kind: "text", excerpt: clip(text, EXCERPT_LIMIT), note: "Text lokal ausgelesen." };
    }
    throw new Error(`${file.name}: unterstützt werden Bilder, PDFs und Textdateien.`);
  }, []);

  const addFiles = useCallback(async (files: FileList | File[]) => {
    const selected = Array.from(files).slice(0, Math.max(0, MAX_ATTACHMENTS - attachments.length));
    if (selected.length === 0) {
      setError(`Es können höchstens ${MAX_ATTACHMENTS} Anhänge pro Nachricht verwendet werden.`);
      return;
    }
    setIsReadingFiles(true);
    setError(null);
    try {
      const loaded: Attachment[] = [];
      for (const file of selected) loaded.push(await readFile(file));
      setAttachments((current) => [...current, ...loaded].slice(0, MAX_ATTACHMENTS));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Anhang konnte nicht gelesen werden.");
    } finally {
      setIsReadingFiles(false);
    }
  }, [attachments.length, readFile]);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((current) => current.filter((attachment) => attachment.id !== id));
  }, []);

  const send = useCallback(async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!activeConversation || isSending || isReadingFiles) return;
    const question = draft.trim() || (attachments.length > 0 ? "Bitte analysiere die angehängten Inhalte." : "");
    if (!question) return;

    const now = new Date().toISOString();
    const userMessage: ChatMessage = {
      id: makeId(), role: "user", content: question, createdAt: now,
      attachments: attachments.map(historyAttachment),
    };
    const history = [...activeConversation.messages, userMessage];
    const conversationId = activeConversation.id;
    setConversations((current) => current.map((conversation) => conversation.id === conversationId ? {
      ...conversation,
      title: conversation.messages.length === 0 ? titleFor(question) : conversation.title,
      updatedAt: now,
      messages: [...conversation.messages, userMessage],
    } : conversation));
    setDraft("");
    setAttachments([]);
    setError(null);
    setIsSending(true);

    try {
      const settings = readStudioSettings();
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: history.map(({ role, content }) => ({ role, content })),
          attachments,
          web_search: webSearch,
          web_search_provider: settings.webSearchProvider,
          llm_url: settings.llmUrl,
          model: settings.model,
          vision_llm_url: settings.visionLlmUrl,
          vision_model: settings.visionModel,
          agent_id: selectedAgent?.id,
          system_prompt: selectedAgent?.systemPrompt,
        }),
      });
      const payload = await response.json().catch(() => null) as { message?: string; sources?: Source[]; runner?: string; detail?: string } | null;
      if (!response.ok || !payload?.message) throw new Error(parseError(payload, "Bonsai-27B hat keine Antwort geliefert."));
      const answer: ChatMessage = {
        id: makeId(), role: "assistant", content: payload.message, createdAt: new Date().toISOString(), sources: payload.sources ?? [], runner: payload.runner,
      };
      setConversations((current) => current.map((conversation) => conversation.id === conversationId ? {
        ...conversation, updatedAt: answer.createdAt, messages: [...conversation.messages, answer],
      } : conversation));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Die Chat-Anfrage ist fehlgeschlagen.");
    } finally {
      setIsSending(false);
    }
  }, [activeConversation, attachments, draft, isReadingFiles, isSending, selectedAgent, webSearch]);

  return (
    <main className="relative min-h-screen px-4 py-5 sm:px-6 lg:px-8">
      <div className="mx-auto max-w-6xl space-y-7 pb-12">
        <SiteNav />
        <div>
          <h1 className="text-3xl font-medium tracking-[-0.04em] text-foreground/80">Chat mit Bonsai-27B</h1>
              <p className="mt-2 text-sm text-muted">Lokale Unterhaltung, lokale Datei-Auszüge und optionale Webrecherche.</p>
        </div>

        <div className="grid min-h-[620px] gap-4 lg:grid-cols-[17rem_minmax(0,1fr)]">
          <aside className="rounded-[1.5rem] border border-border-strong bg-surface-raised p-3 shadow-[var(--panel-shadow)]">
            <Button type="button" onClick={startConversation} className="w-full justify-center"><Plus className="size-4" />Neue Unterhaltung</Button>
            <div className="mt-4 space-y-1">
              {conversations.map((conversation) => (
                <div key={conversation.id} className={cn("group flex items-center gap-1 rounded-xl p-1", activeConversation?.id === conversation.id ? "bg-surface-strong" : "hover:bg-surface-strong/60")}>
                  <button type="button" onClick={() => { setActiveId(conversation.id); setError(null); }} className="min-w-0 flex-1 truncate px-2 py-2 text-left text-xs text-muted-strong" title={conversation.title}>{conversation.title}</button>
                  <button type="button" onClick={() => deleteConversation(conversation.id)} aria-label={`Unterhaltung ${conversation.title} löschen`} className="rounded-lg p-2 text-muted opacity-70 transition hover:bg-background hover:text-red-400 sm:opacity-0 sm:group-hover:opacity-100"><Trash2 className="size-3.5" /></button>
                </div>
              ))}
            </div>
          </aside>

          <section className="flex min-h-[620px] flex-col overflow-hidden rounded-[1.5rem] border border-border-strong bg-surface-raised shadow-[var(--panel-shadow)]">
            <div className="flex items-center gap-2 border-b border-border-strong px-5 py-4 text-sm font-medium text-muted-strong"><MessageCircle className="size-4" />{activeConversation?.title ?? "Neue Unterhaltung"}</div>
            <div className="flex-1 space-y-4 overflow-y-auto px-4 py-5 sm:px-6">
              {activeConversation?.messages.length ? activeConversation.messages.map((message) => (
                <article key={message.id} className={cn("max-w-[90%] rounded-2xl px-4 py-3 text-sm", message.role === "user" ? "ml-auto bg-cta-bg text-cta-ink" : "border border-border bg-surface-strong text-foreground")}>
                  {message.runner ? <p className="mb-2 text-[10px] font-medium uppercase tracking-[0.12em] text-muted">{message.runner}</p> : null}
                  <ReactMarkdown
                    components={{
                      p: ({ children }) => <p className="mb-2 whitespace-pre-wrap leading-6 last:mb-0">{children}</p>,
                      a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer" className="underline underline-offset-2">{children}</a>,
                      ul: ({ children }) => <ul className="mb-2 list-disc space-y-1 pl-5 last:mb-0">{children}</ul>,
                      ol: ({ children }) => <ol className="mb-2 list-decimal space-y-1 pl-5 last:mb-0">{children}</ol>,
                      code: ({ children }) => <code className="rounded bg-background/30 px-1 py-0.5 font-mono text-[0.85em]">{children}</code>,
                      pre: ({ children }) => <pre className="mb-2 overflow-x-auto rounded-lg bg-background/30 p-3 text-xs last:mb-0">{children}</pre>,
                    }}
                  >{message.content}</ReactMarkdown>
                  {message.attachments?.length ? <div className="mt-3 flex flex-wrap gap-2 border-t border-current/15 pt-2 text-xs opacity-85">{message.attachments.map((attachment) => <div key={attachment.id} className="flex max-w-52 items-center gap-2 rounded-lg border border-current/15 px-2 py-1.5">{attachment.kind === "image" && attachment.previewDataUrl ? <NextImage src={attachment.previewDataUrl} alt={`Vorschau von ${attachment.name}`} width={40} height={40} unoptimized className="size-10 rounded object-cover" /> : attachment.kind === "image" ? <ImageIcon className="size-4" /> : <FileText className="size-4" />}<span className="min-w-0"><span className="block truncate">{attachment.name}</span><span className="block text-[10px]">{attachment.note ?? attachment.kind}</span></span></div>)}</div> : null}
                  {message.sources?.length ? <div className="mt-3 space-y-2 border-t border-border pt-2 text-xs text-muted">{message.sources.map((source) => <a key={source.url} href={source.url} target="_blank" rel="noreferrer" className="block hover:text-foreground"><span className="font-medium text-muted-strong">{source.title}</span>{source.snippet ? <span className="block mt-0.5">{source.snippet}</span> : null}</a>)}</div> : null}
                </article>
              )) : <div className="grid h-full place-items-center text-center text-sm text-muted"><div><MessageCircle className="mx-auto mb-3 size-7" />Beginne eine lokale Unterhaltung mit Bonsai-27B.</div></div>}
              {isSending ? <div className="flex items-center gap-2 text-sm text-muted"><LoaderCircle className="size-4 animate-spin" />{selectedAgent ? "Goose-Harness führt den Agenten aus …" : "Bonsai antwortet …"}</div> : null}
              <div ref={messagesEndRef} />
            </div>

            <form ref={formRef} onSubmit={send} className="border-t border-border-strong p-4 sm:p-5">
              {agentProfiles.length ? <div className="mb-3 flex flex-wrap items-center gap-2"><span className="mr-1 text-xs text-muted">Agent:</span><button type="button" onClick={() => setSelectedAgentId(null)} className={cn("rounded-full border px-3 py-1.5 text-xs transition", selectedAgentId === null ? "border-cta-bg bg-cta-bg text-cta-ink" : "border-border bg-surface-strong text-muted hover:text-foreground")}>Allgemein</button>{agentProfiles.map((agent) => <button key={agent.id} type="button" onClick={() => { setSelectedAgentId(agent.id); setWebSearch(agent.webSearchDefault); }} title={agent.description} className={cn("rounded-full border px-3 py-1.5 text-xs transition", selectedAgentId === agent.id ? "border-cta-bg bg-cta-bg text-cta-ink" : "border-border bg-surface-strong text-muted hover:text-foreground")}>{agent.name}</button>)}</div> : null}
              {selectedAgent ? <p className="mb-3 text-xs text-muted">{selectedAgent.description} Dieser Agent läuft über den eingeschränkten Goose-Harness.{selectedAgent.webSearchDefault ? " Webrecherche ist für diesen Agenten voreingestellt." : ""}</p> : null}
              {attachments.length ? <div className="mb-3 flex flex-wrap gap-2">{attachments.map((attachment) => <span key={attachment.id} className="flex max-w-full items-center gap-2 rounded-lg border border-border bg-surface-strong px-2 py-1 text-xs text-muted-strong">{attachment.kind === "image" && attachment.previewDataUrl ? <NextImage src={attachment.previewDataUrl} alt={`Vorschau von ${attachment.name}`} width={32} height={32} unoptimized className="size-8 rounded object-cover" /> : attachment.kind === "image" ? <ImageIcon className="size-3.5" /> : <FileText className="size-3.5" />}<span className="truncate">{attachment.name}</span><button type="button" onClick={() => removeAttachment(attachment.id)} aria-label={`${attachment.name} entfernen`} className="text-muted hover:text-foreground"><X className="size-3" /></button></span>)}</div> : null}
              <Textarea value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); formRef.current?.requestSubmit(); } }} placeholder="Schreibe eine Nachricht …" className="min-h-24 resize-y rounded-xl text-sm leading-6" disabled={isSending || isReadingFiles} />
              {error ? <p role="alert" className="mt-2 text-xs text-red-400">{error}</p> : null}
              <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-3">
                  <input ref={fileInputRef} aria-label="Dateien anhängen" onChange={(event) => { const files = event.currentTarget.files ? Array.from(event.currentTarget.files) : []; event.currentTarget.value = ""; if (files.length) void addFiles(files); }} type="file" multiple accept="image/*,.pdf,text/plain,.txt,.md,.csv,.json,.html,.htm,.xml,.yaml,.yml,.py,.ts,.tsx,.js,.jsx,.css,.sql,.log" className="hidden" />
                  <Button type="button" variant="outline" size="sm" onClick={() => fileInputRef.current?.click()} disabled={isSending || isReadingFiles}><Paperclip className="size-3.5" />{isReadingFiles ? "Lese Anhang …" : "Anhang"}</Button>
                  <label className="flex cursor-pointer items-center gap-2 text-xs text-muted"><input type="checkbox" checked={webSearch} onChange={(event) => setWebSearch(event.target.checked)} disabled={isSending} className="size-4 accent-[var(--accent)]" /><Search className="size-3.5" />Websuche ({readStudioSettings().webSearchProvider === "auto" ? "Auto" : readStudioSettings().webSearchProvider})</label>
                </div>
                <Button type="submit" disabled={isSending || isReadingFiles || (!draft.trim() && attachments.length === 0)}><Send className="size-4" />Senden</Button>
              </div>
              <p className="mt-3 text-[10px] text-muted">Enter sendet, Umschalt-Enter erstellt eine neue Zeile. Text, PDFs und Bilder bleiben lokal. Bei aktivierter Suche wird nur die aktuelle Frage an den in Settings gewählten Anbieter gesendet; Quellen und der tatsächlich verwendete Weg erscheinen in der Antwort. Für FIFA-Spielanfragen ergänzt der öffentliche ESPN-Spielplan aktuelle Paarungen und Ergebnisse.</p>
            </form>
          </section>
        </div>
      </div>
    </main>
  );
}
