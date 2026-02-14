import type { Config } from 'jest';

const config: Config = {
  preset: 'jest-preset-angular',
  setupFiles: ['<rootDir>/setup-jest.ts'],
  testPathIgnorePatterns: ['<rootDir>/node_modules/', '<rootDir>/dist/'],
  maxWorkers: '50%',
  moduleNameMapper: {
    '^lightweight-charts$': '<rootDir>/src/testing/mocks/lightweight-charts.mock.ts',
    '^@polygon\\.io/client-js$': '<rootDir>/src/testing/mocks/polygon-client.mock.ts',
  },
  collectCoverageFrom: [
    'src/app/**/*.ts',
    '!src/app/**/*.routes.ts',
    '!src/app/**/index.ts',
    '!src/main.ts',
  ],
};

export default config;
