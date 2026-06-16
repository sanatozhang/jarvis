import type { Metadata } from "next";
import Script from "next/script";
import { Space_Grotesk, IBM_Plex_Sans, IBM_Plex_Mono } from "next/font/google";
import "./globals.css";
import PageTracker from "@/components/PageTracker";
import LangProvider from "@/components/LangProvider";
import Sidebar from "@/components/Sidebar";
import { AuthProvider } from "@/components/AuthProvider";
import { AuthGate } from "@/components/AuthGate";

// Display — characterful technical grotesque, used with restraint for titles / big numbers / brand
const spaceGrotesk = Space_Grotesk({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-display",
  display: "swap",
});

// Body / UI — engineered humanist that holds up in dense data tables
const plexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-sans",
  display: "swap",
});

// Data — IDs, timestamps, durations, counts; reads like an instrument readout
const plexMono = IBM_Plex_Mono({
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
      <head>
        {/* Apply saved theme before paint to avoid a flash of the wrong mode. */}
        <Script id="theme-init" strategy="beforeInteractive">{`
try {
  var p = new URLSearchParams(location.search).get('theme');
  if (p === 'dark' || p === 'light') localStorage.setItem('apollo_theme', p);
  if ((p || localStorage.getItem('apollo_theme')) === 'dark') document.documentElement.classList.add('dark');
} catch (e) {}
        `}</Script>
        {/*
          浏览器翻译插件（Google/Chrome 翻译）会把文本节点用 <font> 重新包裹/移位，
          React 重渲染时 insertBefore/removeChild 的参照节点父级已被改 → 抛
          "Failed to execute 'insertBefore' on 'Node'" 整页崩溃（线上日语用户实测）。
          翻译插件在 React 管辖外动 DOM，无法阻止，只能给这两个 DOM 操作加防御：
          父级不匹配时跳过而非抛错。必须在 hydration 前执行，故用 beforeInteractive。
          这不会禁用翻译，只是让 React 在被翻译的 DOM 上不崩。
        */}
        <Script id="dom-translate-guard" strategy="beforeInteractive">{`
(function () {
  if (typeof Node !== 'function' || !Node.prototype) return;
  var insertBefore = Node.prototype.insertBefore;
  Node.prototype.insertBefore = function (newNode, referenceNode) {
    if (referenceNode && referenceNode.parentNode !== this) {
      if (newNode) { try { return this.appendChild(newNode); } catch (e) { return newNode; } }
      return newNode;
    }
    return insertBefore.apply(this, arguments);
  };
  var removeChild = Node.prototype.removeChild;
  Node.prototype.removeChild = function (child) {
    if (child && child.parentNode !== this) { return child; }
    return removeChild.apply(this, arguments);
  };
})();
        `}</Script>
      </head>
      <body
        className={`${spaceGrotesk.variable} ${plexSans.variable} ${plexMono.variable} font-sans bg-j-base text-j-fg antialiased`}
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
            </AuthGate>
          </AuthProvider>
        </LangProvider>
      </body>
    </html>
  );
}
