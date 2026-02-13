import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Jarvis - 工单智能分析",
  description: "Plaud 工单 AI 分析平台",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh" suppressHydrationWarning>
      <body className="bg-gray-50 text-gray-900 antialiased" suppressHydrationWarning>
        <div className="flex h-screen">
          {/* Sidebar */}
          <aside className="flex w-56 flex-shrink-0 flex-col border-r border-gray-200 bg-white">
            <div className="flex h-14 items-center gap-2 border-b border-gray-100 px-5">
              <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-black text-xs font-bold text-white">
                J
              </div>
              <span className="text-base font-semibold tracking-tight">Jarvis</span>
            </div>
            <nav className="flex-1 space-y-0.5 px-3 py-3">
              <NavItem href="/" label="工单分析" icon="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
              <NavItem href="/tracking" label="工单跟踪" icon="M15 12a3 3 0 11-6 0 3 3 0 016 0z M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
              <NavItem href="/feedback" label="提交反馈" icon="M12 9v6m3-3H9m12 0a9 9 0 11-18 0 9 9 0 0118 0z" />
              <NavItem href="/oncall" label="值班管理" icon="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
              <NavItem href="/rules" label="分析规则" icon="M4 6h16M4 10h16M4 14h16M4 18h16" />
              <NavItem href="/reports" label="值班报告" icon="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              <NavItem href="/settings" label="系统设置" icon="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </nav>
            <div className="border-t border-gray-100 px-3 py-3">
              <NavItem href="/settings" label="系统状态" icon="M13 10V3L4 14h7v7l9-11h-7z" badge />
            </div>
          </aside>

          {/* Main content */}
          <main className="flex-1 overflow-y-auto">{children}</main>
        </div>
      </body>
    </html>
  );
}

function NavItem({ href, label, icon, badge }: { href: string; label: string; icon: string; badge?: boolean }) {
  return (
    <a
      href={href}
      className="flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900"
    >
      <svg className="h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
        <path strokeLinecap="round" strokeLinejoin="round" d={icon} />
      </svg>
      <span className="flex-1">{label}</span>
      {badge && <span className="h-2 w-2 rounded-full bg-green-400" />}
    </a>
  );
}
