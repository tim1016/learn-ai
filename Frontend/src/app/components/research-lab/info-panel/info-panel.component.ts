import { Component, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AccordionModule } from 'primeng/accordion';

interface FeatureDoc {
  name: string;
  formula: string;
  variables: string;
  interpretation: string;
  window: number;
}

interface TestDoc {
  name: string;
  description: string;
  formula: string;
  interpretation: string;
}

@Component({
  selector: 'app-info-panel',
  standalone: true,
  imports: [CommonModule, AccordionModule],
  templateUrl: './info-panel.component.html',
  styleUrls: ['./info-panel.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InfoPanelComponent {
  features: FeatureDoc[] = [
    {
      name: '5-Minute Momentum',
      formula: 'M₅(t) = (P_t - P_{t-5}) / P_{t-5}',
      variables: 'P_t = close price at bar t',
      interpretation: 'Positive values indicate upward momentum over 5 bars; negative values indicate downward momentum.',
      window: 5,
    },
    {
      name: 'RSI (14)',
      formula: 'RSI = 100 - 100/(1 + RS), where RS = avg_gain(14) / avg_loss(14)',
      variables: 'avg_gain = mean of positive close-to-close changes, avg_loss = mean of negative close-to-close changes',
      interpretation: 'RSI > 70 suggests overbought, RSI < 30 suggests oversold. We test whether extreme RSI predicts forward returns.',
      window: 14,
    },
    {
      name: 'Realized Volatility (30)',
      formula: 'σ₃₀(t) = std(log returns over 30 bars)',
      variables: 'log return = ln(P_t / P_{t-1})',
      interpretation: 'Higher realized volatility indicates larger recent price swings. Can be predictive of mean reversion or continuation.',
      window: 30,
    },
    {
      name: 'Volume Z-Score',
      formula: 'Z(t) = (V_t - μ₂₀) / σ₂₀',
      variables: 'V_t = volume at bar t, μ₂₀ = rolling 20-bar mean volume, σ₂₀ = rolling 20-bar std of volume',
      interpretation: 'Z > 2 indicates unusually high volume. Volume spikes often precede or accompany price moves.',
      window: 20,
    },
    {
      name: 'MACD Signal',
      formula: 'MACD = EMA(12) - EMA(26); Signal = EMA(9, MACD)',
      variables: 'EMA(n) = exponential moving average of close with span n',
      interpretation: 'MACD crossing above signal is bullish; crossing below is bearish. We test the signal line value as a continuous predictor.',
      window: 26,
    },
  ];

  targetDoc = {
    name: '15-Minute Forward Log Return',
    formula: 'R₁₅(t) = ln(P_{t+15} / P_t)',
    description: 'The target variable we predict. Log returns are used because they are additive across time, approximately normally distributed, and symmetrically treat gains and losses. Cross-day boundaries are masked with NaN to prevent contamination.',
  };

  tests: TestDoc[] = [
    {
      name: 'Information Coefficient (IC)',
      description: 'Cross-sectional Spearman rank correlation between feature values and forward returns, computed daily then averaged.',
      formula: 'IC_d = ρ_spearman(feature_d, return_d); Mean IC = avg(IC_d)',
      interpretation: 'Mean IC > 0.03 with t-stat > 1.65 suggests a meaningful signal. IC is the gold standard for alpha factor evaluation.',
    },
    {
      name: 'ADF Stationarity Test',
      description: 'Augmented Dickey-Fuller test checks whether the feature series has a unit root (is non-stationary).',
      formula: 'H₀: unit root exists (non-stationary); reject if p < 0.05',
      interpretation: 'A stationary feature is more reliable for prediction — its statistical properties are stable over time.',
    },
    {
      name: 'Quantile Monotonicity',
      description: 'Sort observations into 5 quantile bins by feature value and compute mean return in each bin.',
      formula: 'E[R|Q_k] for k = 1..5; monotonic if returns increase (or decrease) across bins',
      interpretation: 'Monotonic quantile returns confirm a dose-response relationship: more signal → more return. This is the strongest form of predictive evidence.',
    },
  ];
}
