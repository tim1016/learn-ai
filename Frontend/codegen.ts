import type { CodegenConfig } from '@graphql-codegen/cli';

const config: CodegenConfig = {
  overwrite: true,
  schema: '../contracts/graphql/backend.schema.graphql',
  documents: ['src/app/**/*.graphql'],
  generates: {
    'src/app/graphql/generated/': {
      preset: 'client',
      config: {
        defaultScalarType: 'never',
        strictScalars: true,
        scalars: {
          DateTime: { input: 'string', output: 'string' },
          Decimal: { input: 'number', output: 'number' },
          LocalDate: { input: 'string', output: 'string' },
          Long: { input: 'number', output: 'number' },
          UUID: { input: 'string', output: 'string' },
        },
      },
      presetConfig: {
        fragmentMasking: false,
      },
    },
  },
};

export default config;
