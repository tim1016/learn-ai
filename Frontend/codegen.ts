import type { CodegenConfig } from '@graphql-codegen/cli';

const config: CodegenConfig = {
  overwrite: true,
  schema: '../contracts/graphql/backend.schema.graphql',
  documents: ['src/app/**/*.graphql'],
  // No operation files remain after #1963; the pipeline stays until the Apollo
  // consumers are migrated (removed with #1966), so an empty document set must
  // still regenerate cleanly.
  ignoreNoDocuments: true,
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
