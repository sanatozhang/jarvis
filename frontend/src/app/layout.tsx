import type { Metadata } from "next";
import { Suspense } from "react";
import { DM_Sans, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import PageTracker from "@/components/PageTracker";
import LangProvider from "@/components/LangProvider";
import Sidebar from "@/components/Sidebar";
import { AuthProvider } from "@/components/AuthProvider";
import { AuthGate } from "@/components/AuthGate";
import { FeishuBindPrompt } from "@/components/FeishuBindPrompt";

const dmSans = DM_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-sans",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Apollo — Ticket Analysis",
  description: "Plaud AI Ticket Analysis Platform",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body
        className={`${dmSans.variable} ${jetbrainsMono.variable} font-sans bg-j-base text-j-fg antialiased`}
        suppressHydrationWarning
      >
        <LangProvider>
          <AuthProvider>
            <AuthGate>
              <div className="flex h-screen">
                <Sidebar />
                <main className="flex-1 overflow-y-auto">
                  <PageTracker />
                  {children}
                </main>
              </div>
              <Suspense fallback={null}>
                <FeishuBindPrompt />
              </Suspense>
            </AuthGate>
          </AuthProvider>
        </LangProvider>
      </body>
    </html>
  );
}
