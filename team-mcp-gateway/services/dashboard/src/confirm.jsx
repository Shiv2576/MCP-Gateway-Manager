import { useEffect, useRef, useState } from 'react'

// shadcn AlertDialog in the dashboard's own CSS. Use in place of window.confirm:
//   if (!(await ask({ title: 'Delete X?', description: '…', action: 'Delete' }))) return
let show = null

export function ask({ title, description, action = 'Continue', destructive = true }) {
  return new Promise((resolve) => (show ? show({ title, description, action, destructive, resolve }) : resolve(false)))
}

export function ConfirmHost() {
  const [req, setReq] = useState(null)
  const ref = useRef(null)
  useEffect(() => {
    show = (r) => setReq((prev) => (prev?.resolve(false), r))
    return () => (show = null)
  }, [])
  useEffect(() => {
    if (req && !ref.current.open) ref.current.showModal()
  }, [req])

  function close(ok) {
    req?.resolve(ok)
    setReq(null)
    ref.current.close()
  }

  // Esc fires "cancel"; a click on the backdrop lands on the <dialog> itself.
  return (
    <dialog ref={ref} className="alert-dialog" aria-labelledby="alert-title" aria-describedby="alert-desc"
      onCancel={(e) => (e.preventDefault(), close(false))}
      onClick={(e) => e.target === ref.current && close(false)}>
      {req && (
        <>
          <h2 id="alert-title">{req.title}</h2>
          {req.description && <p id="alert-desc" className="muted">{req.description}</p>}
          <div className="alert-actions">
            <button className="ghost" onClick={() => close(false)} autoFocus>Cancel</button>
            <button className={req.destructive ? 'destructive' : 'primary'} onClick={() => close(true)}>{req.action}</button>
          </div>
        </>
      )}
    </dialog>
  )
}
