import type { Metadata } from "next";
import "./globals.css";
import PageTracker from "@/components/PageTracker";
import LangProvider from "@/components/LangProvider";
import Sidebar from "@/components/Sidebar";

export const metadata: Metadata = {
  title: "Jarvis - Ticket Analysis",
  description: "Plaud AI Ticket Analysis Platform",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="bg-gray-50 text-gray-900 antialiased" suppressHydrationWarning>
        <LangProvider>
          <div className="flex h-screen">
            <Sidebar />
            <main className="flex-1 overflow-y-auto">
              <PageTracker />
              {children}
            </main>
          </div>
        </LangProvider>
      </body>
    </html>
  );
}
