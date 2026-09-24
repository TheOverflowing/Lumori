import { t } from './i18n.js';

let labelSequence = 0;

// Keep native inputs, validation, selection, and form serialization intact.
export function mountInputControls(root) {
  const steppers = new Map();
  const textareas = new Set();
  const dirty = new Set();
  const values = new WeakMap();
  let frame = 0;
  let disposed = false;

  function sizeTextareas() {
    const measuring = [...dirty].filter(input => input.getClientRects().length).map(input => {
      const style = getComputedStyle(input);
      const border = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
      const padding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
      return {input, adjustment:style.boxSizing === 'border-box' ? border : -padding,
        minimum:parseFloat(style.minHeight) || 0, maximum:parseFloat(style.maxHeight) || Infinity};
    });
    // Batch writes, then measurements, then final writes. Never alternate a
    // layout-invalidating height write and forced measurement for every field.
    for (const {input} of measuring) input.style.height = '0px';
    const heights = measuring.map(item => item.input.scrollHeight + item.adjustment);
    measuring.forEach(({input, minimum, maximum}, index) => {
      input.style.height = `${Math.min(maximum, Math.max(minimum, heights[index]))}px`;
      input.style.overflowY = heights[index] > maximum ? 'auto' : 'hidden';
      values.set(input, input.value);
      dirty.delete(input);
    });
  }

  function syncStepper(input, controls) {
    const disabled = input.matches(':disabled') || input.readOnly;
    const value = input.valueAsNumber;
    const label = controls.label?.textContent.trim() || input.getAttribute('aria-label') || '';
    for (const [direction, button] of controls.buttons) {
      button.disabled = disabled;
      const bound = input.getAttribute(direction < 0 ? 'min' : 'max');
      const atBoundary = bound !== null && bound !== '' && Number.isFinite(value)
        && (direction < 0 ? value <= Number(bound) : value >= Number(bound));
      button.setAttribute('aria-disabled', String(disabled || atBoundary));
      button.setAttribute('aria-label', t(direction < 0 ? '减少{label}' : '增加{label}', {label}));
    }
  }

  function addStepper(input) {
    const wrapper = document.createElement('span');
    wrapper.className = 'number-stepper';
    const label = input.labels?.[0]?.querySelector('[data-i18n]');
    if (label && !input.hasAttribute('aria-labelledby') && !input.hasAttribute('aria-label')) {
      label.id ||= `number-label-${++labelSequence}`;
      input.setAttribute('aria-labelledby', label.id);
    }
    input.before(wrapper);
    const buttons = [-1, 1].map(direction => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'stepper-button';
      button.dataset.step = direction;
      button.textContent = direction < 0 ? '−' : '+';
      button.addEventListener('click', () => {
        syncStepper(input, steppers.get(input));
        if (button.getAttribute('aria-disabled') === 'true') return;
        const previous = input.value;
        if (direction < 0) input.stepDown(); else input.stepUp();
        if (input.value !== previous) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
          input.dispatchEvent(new Event('change', {bubbles:true}));
        }
        syncStepper(input, steppers.get(input));
      });
      return [direction, button];
    });
    wrapper.append(buttons[0][1], input, buttons[1][1]);
    steppers.set(input, {buttons, label});
  }

  function refresh(force = true) {
    if (disposed) return;
    for (const input of root.querySelectorAll('input[type="number"]')) {
      if (!steppers.has(input)) addStepper(input);
    }
    for (const [input, controls] of steppers) {
      if (!root.contains(input)) steppers.delete(input);
      else syncStepper(input, controls);
    }
    for (const input of root.querySelectorAll('textarea')) {
      if (!textareas.has(input)) {
        input.dataset.autosize = '';
        textareas.add(input);
        observer.observe(input);
      }
      if (force || values.get(input) !== input.value) dirty.add(input);
    }
    for (const input of textareas) {
      if (!root.contains(input)) { observer.unobserve(input); textareas.delete(input); dirty.delete(input); }
    }
    sizeTextareas();
  }

  function schedule() {
    if (!frame && !disposed) frame = requestAnimationFrame(() => { frame = 0; refresh(false); });
  }
  const widths = new WeakMap();
  const observer = new ResizeObserver(entries => {
    for (const {target, contentRect} of entries) {
      if (widths.get(target) !== contentRect.width) {
        if (widths.has(target)) dirty.add(target);
        widths.set(target, contentRect.width);
        schedule();
      }
    }
  });
  // Bubble after the form's handlers so presets, disabled states and errors are current.
  root.addEventListener('input', schedule);
  root.addEventListener('change', schedule);
  root.addEventListener('click', schedule);
  root.addEventListener('toggle', schedule, true);
  root.addEventListener('reset', schedule);
  refresh();
  return {
    refresh,
    destroy() {
      disposed = true;
      cancelAnimationFrame(frame);
      observer.disconnect();
      root.removeEventListener('input', schedule);
      root.removeEventListener('change', schedule);
      root.removeEventListener('click', schedule);
      root.removeEventListener('toggle', schedule, true);
      root.removeEventListener('reset', schedule);
    },
  };
}
