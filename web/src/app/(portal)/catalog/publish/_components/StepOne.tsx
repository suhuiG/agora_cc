import { CHOICES, type WizardChoice } from "./types";

type StepOneProps = {
  choice: WizardChoice | null;
  onPick: (choice: WizardChoice) => void;
};

export function StepOne({ choice, onPick }: StepOneProps) {
  return (
    <div>
      <p className="text-slate-600 mb-4">
        등록할 자산의 타입과 방식을 골라 주세요.
      </p>
      <div className="space-y-3">
        {CHOICES.map((option) => (
          <button
            key={option.key}
            onClick={() => onPick(option)}
            className={`w-full text-left p-4 border rounded-lg hover:border-slate-400 transition-colors ${
              choice?.key === option.key
                ? "border-blue-500 bg-blue-50"
                : "border-slate-200 bg-white"
            }`}
          >
            <div className="flex items-center gap-2 mb-1">
              <span className="font-semibold">{option.label}</span>
              <span className="text-xs bg-slate-100 text-slate-600 px-2 py-0.5 rounded">
                {option.badge}
              </span>
            </div>
            <span className="text-sm text-slate-500">{option.desc}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
