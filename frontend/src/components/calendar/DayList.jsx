import AppointmentCard from './AppointmentCard.jsx'

/**
 * The appointments of one day under a header. In the agenda the header
 * sticks below the app header while its day scrolls by.
 */
export default function DayList({
  label,
  day,
  items,
  sticky = false,
  patientsById,
  showPatient,
  today,
  onOpen,
  onComplete,
}) {
  const headingId = `day-${day}`
  return (
    <section aria-labelledby={headingId}>
      <h2
        id={headingId}
        className={`bg-slate-50/95 py-2 text-sm font-semibold text-slate-700 ${
          sticky ? 'sticky top-[var(--app-header-h,3.75rem)] z-10 backdrop-blur' : ''
        } ${day === today ? 'text-brand-800' : ''}`}
      >
        <time dateTime={day}>{label}</time>
      </h2>
      <ul className="space-y-2">
        {items.map((appt) => (
          <li key={appt.id}>
            <AppointmentCard
              appt={appt}
              patient={patientsById[appt.patient_id]}
              showPatient={showPatient}
              today={today}
              onOpen={onOpen}
              onComplete={onComplete}
            />
          </li>
        ))}
      </ul>
    </section>
  )
}
