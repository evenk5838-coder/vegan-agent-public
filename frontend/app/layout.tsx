import type { Metadata, Viewport } from 'next';
import './globals.css';
export const metadata: Metadata = { title: '蔬食 · 每天安心一点', description: '看配料、找餐厅、选菜谱、记食材。', manifest: '/manifest.webmanifest' };
export const viewport: Viewport = { themeColor: '#375546', width: 'device-width', initialScale: 1 };
export default function RootLayout({ children }: Readonly<{children: React.ReactNode}>) { return <html lang="zh-CN"><body>{children}</body></html>; }
