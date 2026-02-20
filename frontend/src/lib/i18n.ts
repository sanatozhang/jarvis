/**
 * Lightweight i18n — all UI text in CN/EN.
 * Usage:
 *   const t = useT();
 *   t("工单分析")  → "工单分析" (cn) or "Ticket Analysis" (en)
 */

"use client";

import { createContext, useContext } from "react";

export type Lang = "cn" | "en";

export const LangContext = createContext<Lang>("cn");
export const LangToggleContext = createContext<() => void>(() => {});

export function useT() {
  const lang = useContext(LangContext);
  return (key: string) => {
    if (lang === "cn") return key;
    return EN[key] || key;
  };
}

export function useLang(): Lang {
  return useContext(LangContext);
}

const EN: Record<string, string> = {
  // Sidebar
  "工单分析": "Ticket Analysis",
  "工单跟踪": "Ticket Tracking",
  "提交反馈": "Submit Feedback",
  "值班管理": "On-Call",
  "数据看板": "Analytics",
  "分析规则": "Rules",
  "值班报告": "Reports",
  "系统设置": "Settings",
  "系统状态": "Status",

  // Header
  "全部指派人": "All Assignees",
  "刷新": "Refresh",
  "加载中...": "Loading...",
  "同步飞书": "Sync Feishu",
  "批量分析": "Batch Analyze",
  "设置用户名": "Set Username",

  // Tabs
  "待处理": "Pending",
  "进行中": "In Progress",
  "已完成": "Completed",
  "高优先级": "High Priority",

  // Table headers
  "级别": "Priority",
  "问题描述": "Description",
  "设备 SN": "Device SN",
  "Zendesk": "Zendesk",
  "飞书": "Feishu",
  "状态": "Status",
  "操作": "Actions",
  "提交人": "Submitted by",
  "创建时间": "Created",
  "AI 状态": "AI Status",
  "平台": "Platform",

  // Badges
  "高": "H",
  "低": "L",
  "排队中": "Queued",
  "下载中": "Downloading",
  "解密中": "Decrypting",
  "提取中": "Extracting",
  "分析中": "Analyzing",
  "成功": "Done",
  "分析成功": "Analysis Done",
  "分析失败": "Analysis Failed",
  "失败": "Failed",
  "命中规则": "Matched Rule",
  "已重新加载": "Reloaded",

  // Buttons
  "分析": "Analyze",
  "重试分析": "Retry",
  "重试": "Retry",
  "重新分析": "Re-analyze",
  "复制回复": "Copy Reply",
  "一键复制": "Copy",
  "转工程师": "Escalate",
  "转工程师处理": "Escalate to Engineer",
  "删除": "Delete",
  "确定": "Apply",
  "取消": "Cancel",
  "清除": "Clear",
  "保存": "Save",
  "编辑": "Edit",

  // Detail panel
  "工单详情": "Ticket Detail",
  "AI 分析结果": "AI Analysis",
  "问题原因": "Root Cause",
  "关键证据": "Key Evidence",
  "建议回复": "Suggested Reply",
  "修复建议": "Fix Suggestion",
  "失败原因": "Failure Reason",
  "需工程师": "Engineer Needed",
  "固件": "Firmware",
  "APP": "APP",
  "日志": "Logs",

  // Feedback page
  "手动上传用户问题和日志文件": "Upload user issues and log files",
  "问题分类": "Category",
  "优先级": "Priority",
  "固件版本": "Firmware Version",
  "APP 版本": "APP Version",
  "Zendesk 工单号": "Zendesk Ticket",
  "日志文件": "Log Files",
  "请填写问题描述": "Please enter a description",
  "点击或拖拽上传日志文件": "Click or drag to upload log files",
  "提交中...": "Submitting...",

  // Tracking
  "全部": "All",
  "我的": "Mine",
  "指定人": "By User",
  "筛选": "Filter",
  "清除筛选": "Clear Filters",
  "全部分类": "All Categories",
  "起始日期": "From",
  "结束日期": "To",

  // Analytics
  "项目价值 & 使用情况统计": "Project Value & Usage",
  "项目价值": "Project Value",
  "预估节省工时": "Time Saved",
  "每单节省时间": "Saved per Ticket",
  "分析成功率": "Success Rate",
  "总分析次数": "Total Analyses",
  "反馈提交": "Feedback Submitted",
  "活跃用户": "Active Users",
  "平均分析耗时": "Avg Duration",
  "工单转工程师": "Escalations",
  "页面访问": "Page Visits",
  "每日趋势": "Daily Trend",
  "活跃用户 Top 10": "Top 10 Users",
  "失败原因分布": "Failure Reasons",
  "次操作": "actions",

  // Settings
  "环境配置": "Environment Config",
  "Agent 配置": "Agent Config",
  "Agent 可用性": "Agent Availability",
  "默认 Agent": "Default Agent",
  "超时（秒）": "Timeout (s)",
  "最大轮数": "Max Turns",
  "保存 Agent 配置": "Save Agent Config",
  "保存环境配置": "Save Env Config",
  "配置已生效": "Config applied",

  // Oncall
  "每周轮换，自动通知值班工程师": "Weekly rotation, auto-notify on-call engineers",
  "本周值班": "This Week On-Call",
  "编辑排班": "Edit Schedule",
  "轮换起始日期": "Rotation Start Date",
  "值班分组": "On-Call Groups",
  "添加分组": "Add Group",
  "添加成员": "Add Member",
  "删除分组": "Remove Group",
  "移除": "Remove",
  "本周": "Current",
  "尚未配置值班表": "No on-call schedule configured",

  // Rules
  "重新加载": "Reload",
  "触发关键词": "Trigger Keywords",
  "预提取模式": "Pre-extract Patterns",
  "依赖 & 属性": "Dependencies",
  "已启用": "Enabled",
  "已禁用": "Disabled",

  // Feedback form
  "请选择问题分类": "Select category",
  "上传中": "Uploading",
  "导入": "Import",
  "导入中": "Importing",
  "提交后工单将自动进入 AI 分析": "Ticket will be automatically analyzed by AI after submission",
  "开始 AI 分析": "Start AI Analysis",

  // Feedback extra
  "请详细描述用户遇到的问题...": "Please describe the issue in detail...",
  "输入工单号后点击导入，AI 将自动总结聊天记录并填充表单": "Enter ticket # and click Import. AI will summarize the chat history and fill the form.",
  "复制 Markdown": "Copy Markdown",
  "加载报告中...": "Loading report...",
  "选择日期查看报告": "Select a date to view report",
  "暂无报告": "No reports",
  "总工单数": "Total Tickets",
  "查看原始 Markdown": "View Raw Markdown",
  "用户回复": "User Reply",
  "复制": "Copy",
  "选择一条规则查看详情": "Select a rule to view details",
  "暂未配置值班分组": "No on-call groups configured",
  "只有管理员可以编辑值班排班": "Only admins can edit on-call schedule",
  "该日期暂无已分析工单": "No analyzed tickets for this date",
  "分析工单后，报告会自动生成": "Reports are generated automatically after analysis",
  "暂无工单": "No tickets",

  // Common
  "暂无数据": "No data",
  "暂无待处理工单": "No pending tickets",
  "暂无进行中工单": "No in-progress tickets",
  "暂无已完成工单": "No completed tickets",
  "已复制到剪贴板": "Copied to clipboard",
  "通过飞书消息通知当前值班工程师": "Notify on-call engineer via Feishu message",

  // Username setup
  "欢迎使用 Jarvis": "Welcome to Jarvis",
  "请设置您的用户名，用于标记工单操作": "Set your username to track your actions",
  "输入您的名字": "Enter your name",
  "开始使用": "Get Started",

  // Misc
  "链接": "Link",
  "无": "None",
  "小时": "hours",
  "分钟/单": "min/ticket",
  "分钟": "min",
  "天": "days",
  "个": "",
  "条规则": "rules",
  "上一页": "Prev",
  "下一页": "Next",

  // Analytics extra
  "过去": "past",
  "对比": "Comparison",
  "人工处理": "Manual",
  "AI 处理": "AI",
  "暂无失败记录": "No failures",
  "未知": "Unknown",

  // Tracking extra
  "个工单": "tickets",
  "共": "Total",
  "原因": "Cause",
  "结果": "Result",
  "分类": "Category",
  "用户名": "Username",
  "已重新触发分析": "Re-analysis triggered",
  "重试失败": "Retry failed",
  "已通知": "Notified",
  "通知失败": "Notification failed",

  // Main page extra
  "未知错误": "Unknown error",
  "确定要删除这个工单吗？": "Delete this ticket?",
  "工单已删除": "Ticket deleted",
  "删除失败": "Delete failed",
  "已通知值班工程师": "On-call engineer notified",
  "发送失败": "Send failed",
  "正在从飞书加载工单...": "Loading tickets from Feishu...",
  "首次加载可能需要几秒钟": "First load may take a few seconds",
  "本地上传": "Local upload",

  // Feedback extra
  "请先输入 Zendesk 工单号": "Please enter a Zendesk ticket number first",
  "已导入 Zendesk": "Imported Zendesk",
  "条聊天记录": "messages",
  "导入失败": "Import failed",
  "Zendesk 导入功能暂未配置，请联系管理员设置 Zendesk API 凭证": "Zendesk import is not configured. Please contact the admin to set up Zendesk API credentials.",
  "超过 50MB 限制": "exceeds 50MB limit",
  "文件": "File",
  "请压缩后重试": "please compress and retry",
  "网络错误，请检查网络连接": "Network error, check connection",
  "上传超时（2分钟），请检查文件大小和网络": "Upload timed out (2 min), check file size and network",
  "提交失败": "Submit failed",
  "输入工单号，回车导入": "Enter ticket #, press Enter to import",
  "支持 .plaud, .log, .zip, .gz 格式（单个文件 ≤ 50MB）": "Supports .plaud, .log, .zip, .gz (max 50MB per file)",
  "未上传日志文件": "No Log Files Uploaded",
  "没有日志文件，AI 将无法分析用户的操作行为和设备状态，只能结合代码和产品知识回答问题。": "Without log files, AI cannot analyze user actions or device state. It can only answer based on code and product knowledge.",
  "适用于产品功能咨询、设计逻辑确认等场景。": "Suitable for product feature inquiries and design logic questions.",
  "返回上传日志": "Go Back & Upload Logs",
  "继续提交": "Continue Without Logs",

  // Oncall extra
  "至少需要一组值班人员": "At least one group is required",
  "请设置起始日期": "Please set a start date",
  "值班表已保存": "Schedule saved",
  "保存中...": "Saving...",
  "保存失败": "Save failed",
  "未设置": "Not set",
  "从此日期开始，每周一轮换到下一组": "Starting from this date, rotating weekly",
  "第": "Group",
  "组": "",
  "飞书邮箱，如 engineer@plaud.ai": "Feishu email, e.g. engineer@plaud.ai",

  // Rules extra
  "规则已保存": "Rule saved",
  "切换失败": "Toggle failed",
  "需代码": "Code needed",
  "需要代码": "Needs code",
  "无（兜底规则）": "None (fallback rule)",
  "版本": "Version",
  "依赖": "Dependencies",
  "是": "Yes",
  "否": "No",

  // Reports extra
  "Markdown 已复制": "Markdown copied",
  "今天": "Today",
  "已复制": "Copied",

  // Settings extra
  "Agent 配置已保存": "Agent config saved",
  "没有需要保存的更改": "No changes to save",
  "已保存": "Saved",
  "修改后需要重启服务才能完全生效": "Changes require a restart to take full effect",
  "敏感": "Sensitive",
  "输入新值以更新": "Enter new value to update",
  "环境配置仅管理员可见": "Environment config is admin-only",
  "当前用户": "Current user",
  "未登录": "Not logged in",
  "已安装": "Installed",
  "未安装": "Not installed",
  "检查中...": "Checking...",
  "整体": "Overall",
  "问题类型 → Agent 路由": "Issue Type → Agent Routing",
};
