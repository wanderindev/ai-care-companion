import { createBrowserRouter, Navigate } from 'react-router-dom'
import AppLayout from './components/layout/AppLayout.jsx'
import BitacoraPage from './pages/BitacoraPage.jsx'
import LabsPage from './pages/LabsPage.jsx'
import MedicosPage from './pages/MedicosPage.jsx'
import MedicoPage from './pages/MedicoPage.jsx'
import NotFoundPage from './pages/NotFoundPage.jsx'
import LoginPage from './pages/LoginPage.jsx'
import VerifyPage from './pages/VerifyPage.jsx'
import RequireAuth from './components/auth/RequireAuth.jsx'

export const router = createBrowserRouter([
  // Public: sign-in form and the landing page of the emailed magic link.
  { path: '/entrar', element: <LoginPage /> },
  { path: '/acceso', element: <VerifyPage /> },
  {
    path: '/',
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <Navigate to="/bitacora" replace /> },
      { path: 'bitacora', element: <BitacoraPage /> },
      // Split out: it carries the markdown renderer.
      {
        path: 'bitacora/:entryId',
        lazy: () => import('./pages/BitacoraEntryPage.jsx').then((m) => ({ Component: m.default })),
      },
      // Doctors directory (header menu, not the bottom nav).
      { path: 'medicos', element: <MedicosPage /> },
      { path: 'medicos/:doctorId', element: <MedicoPage /> },
      // Emergency card screen (header menu): big type, tap-to-call, PDF.
      {
        path: 'emergencia',
        lazy: () => import('./pages/EmergenciaPage.jsx').then((m) => ({ Component: m.default })),
      },
      // Admin screen (header menu): upload a WhatsApp export zip.
      {
        path: 'importar-whatsapp',
        lazy: () =>
          import('./pages/ImportarWhatsappPage.jsx').then((m) => ({ Component: m.default })),
      },
      // «Pendientes» (header badge): suggestions to approve; carries the entry,
      // appointment, doctor and medication sheets.
      {
        path: 'pendientes',
        lazy: () => import('./pages/PendientesPage.jsx').then((m) => ({ Component: m.default })),
      },
      // Split out: agenda, month grid and appointment sheet.
      {
        path: 'calendario',
        lazy: () => import('./pages/CalendarioPage.jsx').then((m) => ({ Component: m.default })),
      },
      // Personal ICS feed link + how to subscribe (header menu and Calendario).
      {
        path: 'calendario/suscribir',
        lazy: () =>
          import('./pages/CalendarioSuscribirPage.jsx').then((m) => ({ Component: m.default })),
      },
      // Split out: the medication list, its editor sheets and GFM markdown.
      {
        path: 'medicamentos',
        lazy: () => import('./pages/MedicamentosPage.jsx').then((m) => ({ Component: m.default })),
      },
      // Split out: list versions + bitácora events, and one version's diff.
      {
        path: 'medicamentos/historial',
        lazy: () => import('./pages/MedHistorialPage.jsx').then((m) => ({ Component: m.default })),
      },
      {
        path: 'medicamentos/historial/:versionNo',
        lazy: () => import('./pages/MedVersionPage.jsx').then((m) => ({ Component: m.default })),
      },
      { path: 'labs', element: <LabsPage /> },
      // Admin screen (header menu): analytes flagged for review.
      {
        path: 'labs/revisar-examenes',
        lazy: () => import('./pages/AnalyteReviewPage.jsx').then((m) => ({ Component: m.default })),
      },
      // Split out: report detail, and the analyte page with its trend chart.
      {
        path: 'labs/:reportId',
        lazy: () => import('./pages/LabReportPage.jsx').then((m) => ({ Component: m.default })),
      },
      {
        path: 'labs/examenes/:analyteId',
        lazy: () => import('./pages/AnalytePage.jsx').then((m) => ({ Component: m.default })),
      },
      // Split out: session list, conversation (streaming, markdown), file chips.
      {
        path: 'chat',
        lazy: () => import('./pages/ChatPage.jsx').then((m) => ({ Component: m.default })),
      },
      // `/chat/nueva` is a new conversation; it becomes `/chat/<id>` on the first message.
      {
        path: 'chat/:sessionId',
        lazy: () =>
          import('./pages/ChatConversationPage.jsx').then((m) => ({ Component: m.default })),
      },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
])
