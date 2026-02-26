import {
  ApolloClient,
  InMemoryCache,
  HttpLink,
  ApolloLink,
  from,
  Operation,
  NextLink,
} from '@apollo/client';
import { onError } from '@apollo/client/link/error';
import { RetryLink } from '@apollo/client/link/retry';
import { getAccessToken, clearTokens, refreshAccessToken } from './auth';

// HTTP Link - connects to the GraphQL endpoint
const httpLink = new HttpLink({
  uri: process.env.NEXT_PUBLIC_GRAPHQL_URL || 'http://localhost:8000/graphql/',
  credentials: 'include', // Include cookies for session-based auth
});

// Auth Link - adds JWT token to request headers
const authLink = new ApolloLink((operation: Operation, forward: NextLink) => {
  const token = getAccessToken();
  
  if (token) {
    operation.setContext(({ headers = {} }: { headers: Record<string, string> }) => ({
      headers: {
        ...headers,
        authorization: `Bearer ${token}`,
      },
    }));
  }
  
  return forward(operation);
});

// Error Link - handles and logs errors
const errorLink = onError(({ 
  graphQLErrors, 
  networkError, 
  operation, 
  forward 
}: { 
  graphQLErrors?: readonly { message: string; extensions?: Record<string, unknown> }[];
  networkError?: Error | Record<string, unknown>;
  operation: Operation;
  forward: NextLink;
}) => {
  if (graphQLErrors) {
    for (const error of graphQLErrors) {
      const { message, extensions } = error;
      console.error(`[GraphQL error]: Message: ${message}`);
      
      // Handle authentication errors (401 / UNAUTHENTICATED)
      if (extensions?.code === 'UNAUTHENTICATED' || extensions?.status === 401) {
        console.warn('Authentication token invalid or expired. Attempting refresh...');
        
        // Return an observable that will refresh the token and retry the operation
        return new ApolloLink((op: Operation, fw: NextLink) => {
          // Promise that will resolve with the new token or null
          const refreshPromise = refreshAccessToken();
          
          return from(refreshPromise).flatMap((newToken: string | null) => {
            if (!newToken) {
              clearTokens();
              // Redirect to login if on client side
              if (typeof window !== 'undefined') {
                window.location.href = '/login';
              }
              return from([]);
            }
            
            // Re-set headers with new token
            op.setContext(({ headers = {} }: { headers: Record<string, string> }) => ({
              headers: {
                ...headers,
                authorization: `Bearer ${newToken}`,
              },
            }));
            
            return fw(op);
          });
        }).request(operation, forward);
      }
    }
  }

  if (networkError) {
    console.error(`[Network error]: ${networkError.message}`);
  }
});

// Retry Link - retries failed requests
const retryLink = new RetryLink({
  delay: {
    initial: 300,
    max: 3000,
    jitter: true,
  },
  attempts: {
    max: 3,
    retryIf: (error: Error) => {
      // Retry on network errors but not on GraphQL errors
      return !!error && !error.message.includes('GraphQL error');
    },
  },
});

// Combine all links
const link = from([errorLink, retryLink, authLink, httpLink]);

// Create Apollo Client instance
export const client = new ApolloClient({
  link,
  cache: new InMemoryCache({
    typePolicies: {
      Query: {
        fields: {
          // Add pagination helpers if needed
          events: {
            keyArgs: ['filters'],
            merge(existing: unknown[] = [], incoming: unknown[]) {
              return [...existing, ...incoming];
            },
          },
        },
      },
    },
  }),
  // Enable Apollo DevTools in development
  devtools: {
    enabled: process.env.NODE_ENV === 'development',
  },
  // Default options for queries
  defaultOptions: {
    watchQuery: {
      fetchPolicy: 'cache-and-network',
      errorPolicy: 'all',
    },
    query: {
      fetchPolicy: 'network-only',
      errorPolicy: 'all',
    },
    mutate: {
      errorPolicy: 'all',
    },
  },
});
