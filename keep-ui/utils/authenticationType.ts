
// AuthenticationType.ts

export enum AuthenticationType {
    MULTI_TENANT = "MULTI_TENANT",
    SINGLE_TENANT = "SINGLE_TENANT",
    NO_AUTH = "NO_AUTH"  // Default
}

export const NoAuthUserEmail = "keep";
export const NoAuthTenant = "keep";
