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
  "提交反馈": "Submit",

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
  "数据看板": "Analytics Dashboard",
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
  "系统设置": "System Settings",
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
  "值班管理": "On-Call Management",
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
  "分析规则": "Analysis Rules",
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
  "请选择问题分类": "Select category",

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
};
