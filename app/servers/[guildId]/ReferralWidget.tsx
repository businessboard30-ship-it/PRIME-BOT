// path: app/servers/[guildId]/ReferralWidget.tsx
'use client';

import React, { useState, useEffect } from 'react';
import { Copy, Check, Share2, Users, TrendingUp } from 'lucide-react';

interface ReferralStats {
  clicks: number;
  conversions: number;
  conversion_rate: number;
}

interface ReferralWidgetProps {
  guildId: string;
  refCode: string;
  serverName: string;
}

const ReferralWidget: React.FC<ReferralWidgetProps> = ({ guildId, refCode, serverName }) => {
  const [copied, setCopied] = useState(false);
  const [stats, setStats] = useState<ReferralStats | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetchStats();
  }, [guildId]);

  const fetchStats = async () => {
    setLoading(true);
    try {
      const response = await fetch(`/api/server_analytics/referral-stats?guild_id=${guildId}`);
      const data = await response.json();
      if (data.status === 'ok') {
        setStats(data.referral);
      }
    } catch (error) {
      console.error('Failed to fetch referral stats:', error);
    } finally {
      setLoading(false);
    }
  };

  const baseUrl = typeof window !== 'undefined' ? window.location.origin : 'https://yoursite.com';
  const referralUrl = `${baseUrl}/servers/${guildId}?ref=${refCode}`;

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(referralUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  const handleShare = async (platform: 'discord' | 'twitter' | 'reddit') => {
    const encodedUrl = encodeURIComponent(referralUrl);
    const encodedText = encodeURIComponent(
      `Check out ${serverName} on PRIME-BOT Server Directory! 🚀`
    );

    const urls = {
      discord: `https://discord.com/share?url=${encodedUrl}`,
      twitter: `https://twitter.com/intent/tweet?url=${encodedUrl}&text=${encodedText}`,
      reddit: `https://reddit.com/submit?url=${encodedUrl}&title=${encodedText}`,
    };

    window.open(urls[platform], '_blank', 'width=600,height=400');
  };

  return (
    <div className="mt-8 mb-12 bg-gradient-to-r from-blue-50 to-purple-50 border-2 border-blue-200 rounded-lg p-8">
      <div className="flex items-start justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold text-gray-900 flex items-center gap-2 mb-2">
            <TrendingUp className="text-blue-600" size={28} />
            Refer to Boost Your Server
          </h2>
          <p className="text-gray-700">
            Share your referral link with friends. Each person who joins via your link helps boost your
            server's visibility! 🎉
          </p>
        </div>
        <Share2 className="text-blue-600 opacity-20" size={48} />
      </div>

      {/* Stats Cards */}
      {!loading && stats && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
          <div className="bg-white rounded-lg p-4 border border-gray-200">
            <div className="text-sm font-medium text-gray-600">People Clicked</div>
            <div className="text-3xl font-bold text-blue-600 mt-1">{stats.clicks}</div>
          </div>
          <div className="bg-white rounded-lg p-4 border border-gray-200">
            <div className="text-sm font-medium text-gray-600">Joined Your Server</div>
            <div className="text-3xl font-bold text-green-600 mt-1">{stats.conversions}</div>
            <div className="text-xs text-gray-500 mt-1">
              <Users size={12} className="inline mr-1" />
              {stats.conversion_rate.toFixed(1)}% conversion
            </div>
          </div>
          <div className="bg-white rounded-lg p-4 border border-gray-200">
            <div className="text-sm font-medium text-gray-600">Visibility Boost</div>
            <div className="text-3xl font-bold text-purple-600 mt-1">
              {Math.min(100, Math.round((stats.conversions / 10) * 10))}%
            </div>
            <div className="text-xs text-gray-500 mt-1">
              {stats.conversions < 10
                ? `${10 - stats.conversions} more to featured`
                : 'Featured! ⭐'}
            </div>
          </div>
        </div>
      )}

      {/* Referral Link Section */}
      <div className="bg-white rounded-lg p-6 border border-gray-300 mb-6">
        <label className="block text-sm font-semibold text-gray-900 mb-3">Your Referral Link</label>
        <div className="flex gap-2 flex-col sm:flex-row">
          <input
            type="text"
            value={referralUrl}
            readOnly
            className="flex-1 px-4 py-3 bg-gray-100 border border-gray-300 rounded-lg font-mono text-sm text-gray-700 focus:outline-none"
          />
          <button
            onClick={handleCopy}
            className="flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-700 text-white font-medium py-3 px-6 rounded-lg transition-colors whitespace-nowrap"
          >
            {copied ? (
              <>
                <Check size={18} />
                Copied!
              </>
            ) : (
              <>
                <Copy size={18} />
                Copy Link
              </>
            )}
          </button>
        </div>
        <p className="text-xs text-gray-500 mt-2">
          📌 Tip: Copy and share this link on Discord, Twitter, Reddit, or anywhere to boost your
          server!
        </p>
      </div>

      {/* Share Buttons */}
      <div className="mb-6">
        <label className="block text-sm font-semibold text-gray-900 mb-3">Share On</label>
        <div className="grid grid-cols-3 gap-3">
          <button
            onClick={() => handleShare('discord')}
            className="flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-700 text-white font-medium py-3 px-4 rounded-lg transition-colors"
          >
            <span className="text-lg">💬</span>
            Discord
          </button>
          <button
            onClick={() => handleShare('twitter')}
            className="flex items-center justify-center gap-2 bg-blue-400 hover:bg-blue-500 text-white font-medium py-3 px-4 rounded-lg transition-colors"
          >
            <span className="text-lg">𝕏</span>
            Twitter
          </button>
          <button
            onClick={() => handleShare('reddit')}
            className="flex items-center justify-center gap-2 bg-orange-600 hover:bg-orange-700 text-white font-medium py-3 px-4 rounded-lg transition-colors"
          >
            <span className="text-lg">🤖</span>
            Reddit
          </button>
        </div>
      </div>

      {/* How It Works */}
      <div className="bg-blue-100 border border-blue-300 rounded-lg p-4">
        <h3 className="font-semibold text-blue-900 mb-2">How It Works:</h3>
        <ol className="text-sm text-blue-800 space-y-1">
          <li>✅ Share your referral link with friends or on social media</li>
          <li>✅ When they click and join your server, it counts as a referral</li>
          <li>✅ More referrals = Higher visibility in the directory</li>
          <li>✅ Reach 10 referrals to get featured on the homepage!</li>
        </ol>
      </div>

      {/* Bonus Info */}
      <div className="mt-4 p-4 bg-yellow-50 border border-yellow-200 rounded-lg">
        <p className="text-sm text-yellow-800">
          💡 <strong>Pro Tip:</strong> Combine referrals with positive reviews and high engagement for
          maximum visibility boost!
        </p>
      </div>
    </div>
  );
};

export default ReferralWidget;
