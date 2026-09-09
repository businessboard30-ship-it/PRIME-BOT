// path: app/dashboard/[guildId]/analytics/page.tsx
'use client';

import React, { useState, useEffect } from 'react';
import { LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';
import { TrendingUp, Eye, ThumbsUp, MessageSquare, Share2, Calendar } from 'lucide-react';

interface AnalyticsData {
  server: {
    guild_name: string;
    member_count: number;
    verification_tier: string;
    created_at: string;
  };
  metrics: {
    clicks_total: number;
    clicks_today: number;
    clicks_by_day: Array<{ date: string; count: number }>;
    unique_visitors: number;
    votes_total: number;
    votes_by_day: Array<{ date: string; count: number }>;
    avg_rating: number;
    review_count: number;
    members_estimated: number;
  };
  referral: {
    ref_code: string;
    ref_url: string;
    total_clicks: number;
    conversions: number;
    conversion_rate: number;
    clicks_by_source: {
      direct: number;
      discord: number;
      twitter: number;
      reddit: number;
    };
  };
  verification: {
    current_tier: string;
    next_tier: string;
    qualifications: {
      clicks_30d: number;
      votes_total: number;
      avg_rating: number;
      age_days: number;
      auto_verified: boolean;
    };
  };
}

interface AnalyticsDashboardProps {
  params: { guildId: string };
  searchParams: { token?: string; days?: string };
}

const AnalyticsDashboard: React.FC<AnalyticsDashboardProps> = ({ params, searchParams }) => {
  const [analytics, setAnalytics] = useState<AnalyticsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState(30);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchAnalytics();
  }, [params.guildId, days]);

  const fetchAnalytics = async () => {
    setLoading(true);
    setError(null);
    try {
      const token = searchParams.token || localStorage.getItem(`listing_token_${params.guildId}`);
      const response = await fetch(
        `/api/server_analytics?token=${token}&guild_id=${params.guildId}&days=${days}`
      );
      const data = await response.json();
      
      if (data.status === 'ok') {
        setAnalytics(data);
      } else {
        setError(data.message || 'Failed to load analytics');
      }
    } catch (err) {
      console.error('Analytics error:', err);
      setError('Failed to load analytics data');
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600" />
      </div>
    );
  }

  if (error || !analytics) {
    return (
      <div className="min-h-screen bg-red-50 flex items-center justify-center">
        <div className="bg-white p-8 rounded-lg shadow-lg border border-red-200">
          <h1 className="text-red-800 text-xl font-bold mb-2">Error Loading Analytics</h1>
          <p className="text-red-600">{error || 'Unable to load your analytics data'}</p>
          <button
            onClick={fetchAnalytics}
            className="mt-4 bg-red-600 hover:bg-red-700 text-white px-6 py-2 rounded-lg"
          >
            Try Again
          </button>
        </div>
      </div>
    );
  }

  const MetricCard: React.FC<{
    icon: React.ReactNode;
    label: string;
    value: string | number;
    change?: string;
  }> = ({ icon, label, value, change }) => (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <div className="flex items-center justify-between mb-2">
        <span className="text-gray-600 text-sm font-medium">{label}</span>
        <div className="text-blue-600 opacity-50">{icon}</div>
      </div>
      <div className="text-3xl font-bold text-gray-900">{value}</div>
      {change && <div className="text-xs text-green-600 mt-1">↑ {change}</div>}
    </div>
  );

  const VerificationStatus = () => {
    const { current_tier, qualifications } = analytics.verification;
    const tierColors = {
      unverified: 'bg-gray-100 text-gray-800',
      auto_verified: 'bg-blue-100 text-blue-800',
      manual_verified: 'bg-purple-100 text-purple-800',
      gold_verified: 'bg-yellow-100 text-yellow-800',
      platinum_verified: 'bg-pink-100 text-pink-800',
    } as Record<string, string>;

    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <h3 className="text-lg font-semibold text-gray-900 mb-4">Verification Status</h3>
        <div className={`inline-block px-4 py-2 rounded-full font-semibold mb-4 ${tierColors[current_tier] || tierColors.unverified}`}>
          {current_tier.replace(/_/g, ' ').toUpperCase()}
        </div>

        <div className="space-y-3 text-sm">
          <div className="flex justify-between">
            <span className="text-gray-600">Clicks (30 days):</span>
            <span className="font-semibold text-gray-900">
              {qualifications.clicks_30d} / 50
              <div className="w-32 bg-gray-200 rounded-full h-2 mt-1">
                <div
                  className="bg-blue-600 h-2 rounded-full"
                  style={{ width: `${Math.min(100, (qualifications.clicks_30d / 50) * 100)}%` }}
                />
              </div>
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-600">Total Votes:</span>
            <span className="font-semibold text-gray-900">
              {qualifications.votes_total} / 25
              <div className="w-32 bg-gray-200 rounded-full h-2 mt-1">
                <div
                  className="bg-green-600 h-2 rounded-full"
                  style={{ width: `${Math.min(100, (qualifications.votes_total / 25) * 100)}%` }}
                />
              </div>
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-600">Listing Age:</span>
            <span className="font-semibold text-gray-900">
              {qualifications.age_days} / 30 days
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-600">Avg Rating:</span>
            <span className="font-semibold text-gray-900">
              {qualifications.avg_rating ? `${qualifications.avg_rating}/5` : 'N/A'} / 3.5★
            </span>
          </div>
        </div>

        {current_tier === 'unverified' && qualifications.auto_verified && (
          <div className="mt-4 p-3 bg-green-50 border border-green-200 rounded text-green-800 text-sm">
            ✓ Your server qualifies for auto-verification! Refresh to apply.
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="min-h-screen bg-gray-50 p-8">
      <div className="max-w-7xl mx-auto">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-3">
            <TrendingUp className="text-blue-600" size={32} />
            {analytics.server.guild_name} - Analytics
          </h1>
          <p className="text-gray-600 mt-2">Members: {analytics.server.member_count}</p>
        </div>

        {/* Date Range Selector */}
        <div className="mb-6 flex gap-2">
          {[7, 30, 90].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`px-4 py-2 rounded-lg font-medium transition-colors ${
                days === d
                  ? 'bg-blue-600 text-white'
                  : 'bg-white text-gray-800 border border-gray-200 hover:bg-gray-50'
              }`}
            >
              {d} Days
            </button>
          ))}
        </div>

        {/* Key Metrics */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
          <MetricCard
            icon={<Eye size={24} />}
            label="Total Clicks"
            value={analytics.metrics.clicks_total}
            change={`${analytics.metrics.clicks_today} today`}
          />
          <MetricCard
            icon={<Users size={24} />}
            label="Unique Visitors"
            value={analytics.metrics.unique_visitors}
          />
          <MetricCard
            icon={<ThumbsUp size={24} />}
            label="Total Votes"
            value={analytics.metrics.votes_total}
          />
          <MetricCard
            icon={<MessageSquare size={24} />}
            label="Reviews"
            value={`${analytics.metrics.review_count} (${analytics.metrics.avg_rating?.toFixed(1) || 'N/A'}★)`}
          />
        </div>

        {/* Verification & Referral Status */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
          <VerificationStatus />

          {/* Referral Stats */}
          <div className="bg-white rounded-lg border border-gray-200 p-6">
            <h3 className="text-lg font-semibold text-gray-900 mb-4 flex items-center gap-2">
              <Share2 size={20} />
              Referral Performance
            </h3>
            <div className="space-y-4">
              <div>
                <div className="flex justify-between mb-1">
                  <span className="text-sm font-medium text-gray-700">Total Clicks</span>
                  <span className="font-bold text-gray-900">{analytics.referral.total_clicks}</span>
                </div>
              </div>
              <div>
                <div className="flex justify-between mb-1">
                  <span className="text-sm font-medium text-gray-700">Conversions</span>
                  <span className="font-bold text-green-600">{analytics.referral.conversions}</span>
                </div>
              </div>
              <div>
                <div className="flex justify-between mb-1">
                  <span className="text-sm font-medium text-gray-700">Conversion Rate</span>
                  <span className="font-bold text-blue-600">{analytics.referral.conversion_rate.toFixed(2)}%</span>
                </div>
              </div>
              <div className="pt-4 border-t border-gray-200">
                <p className="text-xs font-medium text-gray-600 mb-2">CLICKS BY SOURCE</p>
                <div className="space-y-1 text-sm">
                  <div className="flex justify-between">
                    <span>Direct</span>
                    <span className="font-semibold">{analytics.referral.clicks_by_source.direct}</span>
                  </div>
                  <div className="flex justify-between">
                    <span>Discord</span>
                    <span className="font-semibold">{analytics.referral.clicks_by_source.discord}</span>
                  </div>
                  <div className="flex justify-between">
                    <span>Twitter</span>
                    <span className="font-semibold">{analytics.referral.clicks_by_source.twitter}</span>
                  </div>
                  <div className="flex justify-between">
                    <span>Reddit</span>
                    <span className="font-semibold">{analytics.referral.clicks_by_source.reddit}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Charts */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Clicks Chart */}
          <div className="bg-white rounded-lg border border-gray-200 p-6">
            <h3 className="text-lg font-semibold text-gray-900 mb-4">Listing Clicks Over Time</h3>
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={analytics.metrics.clicks_by_day}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="date" />
                <YAxis />
                <Tooltip />
                <Line type="monotone" dataKey="count" stroke="#3B82F6" strokeWidth={2} />
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* Votes Chart */}
          <div className="bg-white rounded-lg border border-gray-200 p-6">
            <h3 className="text-lg font-semibold text-gray-900 mb-4">Votes Over Time</h3>
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={analytics.metrics.votes_by_day}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="date" />
                <YAxis />
                <Tooltip />
                <Bar dataKey="count" fill="#10B981" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>
    </div>
  );
};

export default AnalyticsDashboard;
