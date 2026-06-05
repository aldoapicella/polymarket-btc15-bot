"use client";

import clsx from "clsx";
import { Activity, FileText, LayoutDashboard, PanelsTopLeft, Settings, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

const nav = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/markets", label: "Markets", icon: PanelsTopLeft },
  { href: "/reports", label: "Reports", icon: FileText },
  { href: "/config", label: "Config", icon: Settings }
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-line bg-[#f1f3ee]/92 backdrop-blur">
        <div className="mx-auto flex max-w-[1440px] items-center justify-between gap-4 px-4 py-3 lg:px-6">
          <Link href="/dashboard" className="flex min-w-0 items-center gap-3">
            <span className="grid h-9 w-9 shrink-0 place-items-center border border-ink bg-ink text-white">
              <Activity className="h-4 w-4" />
            </span>
            <span className="min-w-0">
              <span className="block truncate text-sm font-semibold uppercase text-ink">
                PolyEdge Control Plane
              </span>
              <span className="block truncate text-xs text-ink/60">Paper-first operations</span>
            </span>
          </Link>

          <nav className="flex items-center gap-1 rounded border border-line bg-white p-1 shadow-hairline">
            {nav.map((item) => {
              const active = pathname.startsWith(item.href);
              const Icon = item.icon;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={clsx(
                    "flex h-9 items-center gap-2 rounded-sm px-3 text-sm font-medium transition",
                    active ? "bg-ink text-white" : "text-ink/70 hover:bg-panel hover:text-ink"
                  )}
                >
                  <Icon className="h-4 w-4" />
                  <span className="hidden sm:inline">{item.label}</span>
                </Link>
              );
            })}
          </nav>

          <div className="hidden items-center gap-2 text-xs text-ink/60 md:flex">
            <ShieldCheck className="h-4 w-4 text-good" />
            <span>Live gates backend-only</span>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1440px] px-4 py-5 lg:px-6">{children}</main>
    </div>
  );
}
