/** Browser check for PythonDataService/app/broker/contract/models.py::US_EQUITY_SYMBOL_PATTERN. */
const US_EQUITY_SYMBOL_PATTERN = /^[A-Z]{1,5}(?:[.-][A-Z])?$/;

export function isUsEquitySymbol(value: string): boolean {
  const match = US_EQUITY_SYMBOL_PATTERN.exec(value);
  return match !== null && match[0].length === value.length;
}
