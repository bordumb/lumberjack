import { Suspense } from "react";
import type { Metadata } from "next";
import { Nav } from "@/components/nav";
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
    <html
      lang="en"
      className={`dark ${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full">
        <div className="flex h-screen">
          <Suspense fallback={<div className="w-60 shrink-0 border-r border-border" />}>
            <Nav />
          </Suspense>
          <div className="min-w-0 flex-1 overflow-auto">{children}</div>
        </div>
      </body>
    </html>
  );
}
