import type { Metadata } from "next";
import Script from "next/script";
import { DM_Sans, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import PageTracker from "@/components/PageTracker";
import LangProvider from "@/components/LangProvider";
import Sidebar from "@/components/Sidebar";
import { AuthProvider } from "@/components/AuthProvider";
import { AuthGate } from "@/components/AuthGate";

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
      <head>
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
            </AuthGate>
          </AuthProvider>
        </LangProvider>
      </body>
    </html>
  );
}
