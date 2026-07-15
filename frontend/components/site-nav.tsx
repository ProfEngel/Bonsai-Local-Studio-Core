"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Settings } from "lucide-react";
import { cn } from "@/lib/utils";
import { ThemeToggle } from "@/components/theme-toggle";

export function SiteNav() {
  const pathname = usePathname();

  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <Link href="/" aria-label="Bonsai home">
        <span
          role="img"
          aria-label="Bonsai"
          className="block h-8 w-[10rem] bg-accent"
          style={{
            WebkitMaskImage: "url('/brand/bonsai-logo-horizontal-dark.svg')",
            maskImage: "url('/brand/bonsai-logo-horizontal-dark.svg')",
            WebkitMaskRepeat: "no-repeat",
            maskRepeat: "no-repeat",
            WebkitMaskSize: "contain",
            maskSize: "contain",
            WebkitMaskPosition: "left center",
            maskPosition: "left center",
          }}
        />
      </Link>

      <nav aria-label="Studio views" className="order-3 flex rounded-xl border border-border-strong bg-surface-strong p-1 sm:order-none">
        <Link
          href="/"
          className={cn(
            "rounded-lg px-3 py-1.5 text-xs font-medium transition",
            pathname === "/" ? "bg-background text-foreground shadow-sm" : "text-muted hover:text-foreground",
          )}
        >
          Bilder erstellen
        </Link>
        <Link
          href="/chat"
          className={cn(
            "rounded-lg px-3 py-1.5 text-xs font-medium transition",
            pathname === "/chat" ? "bg-background text-foreground shadow-sm" : "text-muted hover:text-foreground",
          )}
        >
          Chat
        </Link>
      </nav>

      <div className="flex items-center gap-1">
        <Link
          href="/settings"
          aria-label="Studio settings"
          className="flex size-9 items-center justify-center rounded-lg text-muted transition hover:bg-surface-raised hover:text-foreground"
        >
          <Settings className="size-4" />
        </Link>
        <ThemeToggle />
      </div>
    </div>
  );
}
