import { gql } from "apollo-angular";

export const GET_BOOKS = gql`
  query GetBooks {
    books {
      id
      title
      publishedYear
      author {
        id
        name
      }
    }
  }
`;

export const GET_AUTHORS = gql`
  query GetAuthors {
    authors {
      id
      name
      bio
      books {
        id
        title
        publishedYear
      }
    }
  }
`;

export const GET_OR_FETCH_STOCK_AGGREGATES = gql`
  query GetOrFetchStockAggregates(
    $ticker: String!
    $fromDate: String!
    $toDate: String!
    $timespan: String! = "day"
    $multiplier: Int! = 1
  ) {
    getOrFetchStockAggregates(
      ticker: $ticker
      fromDate: $fromDate
      toDate: $toDate
      timespan: $timespan
      multiplier: $multiplier
    ) {
      ticker
      aggregates {
        id
        open
        high
        low
        close
        volume
        volumeWeightedAveragePrice
        timestamp
        timespan
        multiplier
        transactionCount
      }
      summary {
        periodHigh
        periodLow
        averageVolume
        averageVwap
        openPrice
        closePrice
        priceChange
        priceChangePercent
        totalBars
      }
    }
  }
`;
