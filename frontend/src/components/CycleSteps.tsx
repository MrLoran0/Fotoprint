const cycle = ['Найдите сигнал в данных', 'Подтвердите решение и поручения', 'Проверьте результат'] as const

export function CycleSteps({ steps = cycle }: { steps?: readonly string[] }) {
  return <ol className="cycle-steps">{steps.map((step, index) =>
    <li key={step}><span aria-hidden="true">{index + 1}</span>{step}</li>,
  )}</ol>
}
