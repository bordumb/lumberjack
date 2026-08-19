import { Suspense } from "react";
import type { Metadata } from "next";
import { Nav } from "@/components/nav";
import { ThemeProvider } from "@/components/theme-provider";
import { THEME_BOOTSTRAP } from "@/lib/theme";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Lumberjack",
  description: "Live monitoring for a swarm of agents in parallel git worktrees",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // The bootstrap script below rewrites this element's class before React hydrates,
    // which is a mismatch by construction rather than a bug worth reporting.
    <html
      lang="en"
      suppressHydrationWarning
      className={`dark ${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP }} />
      </head>
      <body className="min-h-full">
        <ThemeProvider>
          <div className="flex h-screen">
            <Suspense fallback={<div className="w-64 shrink-0 border-r border-border/60" />}>
              <Nav />
            </Suspense>
            <div className="min-w-0 flex-1 overflow-auto">{children}</div>
          </div>
        </ThemeProvider>
      </body>
    </html>
  );
}
