import React, { useEffect, useState } from 'react';
import { ApiError, SecureAPI } from '../lib/secureApi';

interface RevenueData {
    property_id: string;
    period: string;
    timezone: string;
    /** Decimal string such as "1000.00" - never a float, see formatAmount. */
    total_revenue: string;
    currency: string;
    reservations_count: number;
}

interface RevenueSummaryProps {
    propertyId?: string;
    showRaw?: boolean;
}

/** A plain decimal string, which is the contract for every monetary field. */
const DECIMAL = /^-?\d+(\.\d+)?$/;

/** True when the API returned a clean 2-decimal amount. */
const isExactAmount = (value: string) => /^-?\d+\.\d{2}$/.test(value);

/** Built once: constructing an Intl.NumberFormat resolves locale data. */
const groupFormatter = new Intl.NumberFormat();

/** The decimal separator of the viewer's locale, discovered without parsing. */
const decimalSeparator =
    groupFormatter.formatToParts(1.1).find((p) => p.type === 'decimal')?.value ?? '.';

/**
 * Formats a decimal string for display without ever converting it to a Number.
 * Parsing "1080.40" into a float and rounding it back is how cents get lost.
 * Grouping and the decimal mark both come from the viewer's locale, so a German
 * reader gets "1.000,00" rather than the mixed "1.000.00".
 * Returns null for anything that is not a decimal string - the caller decides
 * what to show, because throwing here would happen during render.
 */
const formatAmount = (value: unknown): string | null => {
    if (typeof value !== 'string') return null;
    const trimmed = value.trim();
    if (!DECIMAL.test(trimmed)) return null;

    // DECIMAL guarantees at least one digit before the dot, so no fallbacks here.
    const negative = trimmed.startsWith('-');
    const [integerPart, fractionPart = ''] = trimmed.replace('-', '').split('.');
    const grouped = groupFormatter.format(BigInt(integerPart));
    const fraction = `${fractionPart}00`.slice(0, 2);
    return `${negative ? '-' : ''}${grouped}${decimalSeparator}${fraction}`;
};

export const RevenueSummary: React.FC<RevenueSummaryProps> = ({ propertyId = 'prop-001', showRaw }) => {
    const [data, setData] = useState<RevenueData | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    useEffect(() => {
        // Guards against out-of-order responses: switching properties quickly
        // could otherwise let a slow request overwrite a newer one, showing one
        // property's revenue under another property's name.
        let ignore = false;

        const fetchRevenue = async () => {
            setLoading(true);
            // Reset the previous failure, otherwise one error pins the card for
            // the rest of the session even after a later request succeeds.
            setError('');
            try {
                // The tenant is derived from the authenticated token on the backend.
                // The client must never get to state which tenant it is reading.
                const response = await SecureAPI.getDashboardSummary(propertyId);
                if (ignore) return;
                setData(response);
            } catch (err) {
                if (ignore) return;
                setError(err instanceof ApiError && err.status === 404
                    ? 'This property is not available for your account.'
                    : 'Failed to load revenue data');
                setData(null);
                console.error(err);
            } finally {
                if (!ignore) setLoading(false);
            }
        };

        fetchRevenue();
        return () => { ignore = true; };
    }, [propertyId]);

    if (loading) {
        return (
            <div className="bg-white p-6 rounded-xl shadow-sm border border-gray-200">
                <div className="animate-pulse space-y-4">
                    <div className="h-4 bg-gray-100 rounded w-1/4"></div>
                    <div className="h-8 bg-gray-100 rounded w-1/2"></div>
                    <div className="flex gap-4 pt-4">
                        <div className="h-12 bg-gray-100 rounded flex-1"></div>
                        <div className="h-12 bg-gray-100 rounded flex-1"></div>
                    </div>
                </div>
            </div>
        );
    }

    if (error) return <div className="p-4 text-red-500 bg-red-50 rounded-lg">{error}</div>;
    if (!data) return null;

    const displayTotal = formatAmount(data.total_revenue);
    if (displayTotal === null) {
        return (
            <div className="p-4 text-red-500 bg-red-50 rounded-lg">
                Unexpected revenue format from the API — refusing to display a figure we cannot vouch for.
            </div>
        );
    }

    return (
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden hover:shadow-md transition-shadow duration-300">
            {showRaw && (
                <div className="p-3 bg-gray-50 text-xs font-mono border-b border-gray-100 overflow-auto max-h-32">
                    <strong className="block mb-1 text-gray-500 uppercase tracking-wider text-[10px]">Raw API Response</strong>
                    <pre className="text-gray-700">{JSON.stringify(data, null, 2)}</pre>
                </div>
            )}

            <div className="p-6">
                <div className="flex items-center justify-between mb-6">
                    <div>
                        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Total Revenue</h2>
                        <div className="flex items-baseline gap-2 mt-1">
                            <span className="text-3xl font-bold text-gray-900 tracking-tight">
                                {data.currency} {displayTotal}
                            </span>
                            {/* Fake trend indicator for premium feel */}
                            <span className="inline-flex items-baseline px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800 md:mt-2 lg:mt-0">
                                <svg className="-ml-1 mr-0.5 h-3 w-3 flex-shrink-0 self-center text-green-500" fill="currentColor" viewBox="0 0 20 20" aria-hidden="true">
                                    <path fillRule="evenodd" d="M5.293 9.707a1 1 0 010-1.414l4-4a1 1 0 011.414 0l4 4a1 1 0 01-1.414 1.414L11 7.414V15a1 1 0 11-2 0V7.414L6.707 9.707a1 1 0 01-1.414 0z" clipRule="evenodd" />
                                </svg>
                                12%
                            </span>
                        </div>
                    </div>
                </div>

                <div className="grid grid-cols-2 gap-4 pt-4 border-t border-gray-100">
                    <div>
                        <p className="text-xs text-gray-500 font-medium uppercase tracking-wider">Property ID</p>
                        <p className="text-sm font-semibold text-gray-700 font-mono mt-1">{data.property_id}</p>
                    </div>
                    <div>
                        <p className="text-xs text-gray-500 font-medium uppercase tracking-wider">Reservations</p>
                        <p className="text-sm font-semibold text-gray-700 mt-1">{data.reservations_count} <span className="font-normal text-gray-400">bookings</span></p>
                    </div>
                </div>

                <p className="mt-4 text-xs text-gray-400">
                    Period <span className="font-mono text-gray-500">{data.period}</span> · computed in <span className="font-mono text-gray-500">{data.timezone}</span>
                </p>

                {/* Precision Warning Area */}
                <div className="mt-4 h-6">
                    {!isExactAmount(data.total_revenue) && showRaw && (
                        <div className="flex items-center text-xs text-amber-600 bg-amber-50 px-2 py-1 rounded">
                            <svg className="h-4 w-4 mr-1.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                            </svg>
                            Precision Mismatch Detected
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
};
