// path: app/servers/[guildId]/ReviewsSection.tsx
'use client';

import React, { useState, useEffect } from 'react';
import { Star, MessageSquare, ThumbsUp } from 'lucide-react';

interface Review {
  review_id: number;
  rating: number;
  review_text: string;
  helpful_count: number;
  is_verified_member: boolean;
  review_date: string;
  reviewer_user_id: number;
}

interface ReviewsResponse {
  status: string;
  reviews: Review[];
  total: number;
  avg_rating: number;
  review_count: number;
  rating_distribution: Record<number, number>;
}

// Hoisted OUTSIDE ReviewsSection: defining a component inline inside another
// component's function body gives it a brand-new identity on every render of
// the parent. React then treats it as a different component type each time
// and unmounts/remounts the whole subtree instead of just updating props —
// which is what made the stars (and anything below them) flash and vanish
// for an instant on every tap, since tapping a star itself triggers the
// parent re-render (setUserRating) that recreated this component.
const RatingStars: React.FC<{ rating: number; interactive?: boolean; onRate?: (r: number) => void }> = ({
  rating,
  interactive = false,
  onRate,
}) => (
  <div className="flex gap-1">
    {[1, 2, 3, 4, 5].map((star) => (
      <Star
        key={star}
        size={interactive ? 28 : 16}
        className={`${
          star <= rating ? 'fill-yellow-400 text-yellow-400' : 'text-gray-300'
        } ${interactive ? 'cursor-pointer' : ''}`}
        onClick={() => interactive && onRate?.(star)}
      />
    ))}
  </div>
);

const ReviewsSection: React.FC<{ guildId: string; refCode?: string }> = ({ guildId, refCode }) => {
  const [reviews, setReviews] = useState<Review[]>([]);
  const [avgRating, setAvgRating] = useState<number>(0);
  const [reviewCount, setReviewCount] = useState<number>(0);
  const [ratingDistribution, setRatingDistribution] = useState<Record<number, number>>({});
  const [loading, setLoading] = useState(true);
  const [showReviewForm, setShowReviewForm] = useState(false);
  const [sort, setSort] = useState<'recent' | 'helpful' | 'rating'>('recent');
  const [userRating, setUserRating] = useState(0);
  const [userReviewText, setUserReviewText] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [helpfulPending, setHelpfulPending] = useState<Set<number>>(new Set());

  useEffect(() => {
    fetchReviews();
  }, [guildId, sort]);

  const fetchReviews = async () => {
    setLoading(true);
    try {
      const response = await fetch(
        `/api/server_reviews?guild_id=${guildId}&sort=${sort}&page=1&page_size=10`
      );
      const data: ReviewsResponse = await response.json();
      
      if (data.status === 'ok') {
        setReviews(data.reviews);
        setAvgRating(data.avg_rating || 0);
        setReviewCount(data.review_count || 0);
        setRatingDistribution(data.rating_distribution || {});
      }
    } catch (error) {
      console.error('Failed to fetch reviews:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleSubmitReview = async () => {
    if (userRating === 0) {
      alert('Please select a rating');
      return;
    }

    setSubmitting(true);
    try {
      const response = await fetch(`/api/server_reviews?guild_id=${guildId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          rating: userRating,
          review_text: userReviewText,
          discord_user_id: (window as any).discordUserId, // Set from OAuth
          is_verified_member: false,
        }),
      });

      const data = await response.json();
      if (data.status === 'ok') {
        setShowReviewForm(false);
        setUserRating(0);
        setUserReviewText('');
        fetchReviews();
      } else {
        alert(`Error: ${data.message}`);
      }
    } catch (error) {
      console.error('Failed to submit review:', error);
      alert('Failed to submit review');
    } finally {
      setSubmitting(false);
    }
  };

  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

    if (diffDays === 0) return 'Today';
    if (diffDays === 1) return 'Yesterday';
    if (diffDays < 7) return `${diffDays} days ago`;
    if (diffDays < 30) return `${Math.floor(diffDays / 7)} weeks ago`;
    return `${Math.floor(diffDays / 30)} months ago`;
  };

  const handleMarkHelpful = async (reviewId: number) => {
    if (helpfulPending.has(reviewId)) return; // already in flight for this review
    setHelpfulPending((prev) => new Set(prev).add(reviewId));
    // Optimistic update — bump the count immediately instead of waiting on
    // a full fetchReviews() round trip, so the tap feels instant.
    setReviews((prev) =>
      prev.map((r) => (r.review_id === reviewId ? { ...r, helpful_count: r.helpful_count + 1 } : r))
    );
    try {
      const response = await fetch(`/api/server_reviews?review_id=${reviewId}&helpful=1`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const data = await response.json();
      if (data.status !== 'ok') {
        // Roll back the optimistic bump on failure.
        setReviews((prev) =>
          prev.map((r) => (r.review_id === reviewId ? { ...r, helpful_count: r.helpful_count - 1 } : r))
        );
      }
    } catch (error) {
      console.error('Failed to mark review helpful:', error);
      setReviews((prev) =>
        prev.map((r) => (r.review_id === reviewId ? { ...r, helpful_count: r.helpful_count - 1 } : r))
      );
    } finally {
      setHelpfulPending((prev) => {
        const next = new Set(prev);
        next.delete(reviewId);
        return next;
      });
    }
  };

  return (
    <div className="mt-12 border-t border-gray-200 pt-8">
      {/* Rating Summary */}
      <div className="mb-8">
        <h2 className="text-2xl font-bold mb-6 flex items-center gap-2">
          <Star className="fill-yellow-400 text-yellow-400" size={28} />
          Community Reviews
        </h2>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-8 mb-8">
          {/* Left: Large Rating Display */}
          <div className="flex flex-col items-center justify-center bg-gradient-to-br from-blue-50 to-purple-50 rounded-lg p-6 border border-gray-200">
            <div className="text-5xl font-bold text-gray-900 mb-2">{avgRating.toFixed(1)}</div>
            <RatingStars rating={Math.round(avgRating)} />
            <div className="text-sm text-gray-600 mt-2">{reviewCount} reviews</div>
          </div>

          {/* Right: Rating Distribution */}
          <div className="md:col-span-2 space-y-2">
            {[5, 4, 3, 2, 1].map((star) => {
              const count = ratingDistribution[star] || 0;
              const percentage = reviewCount > 0 ? (count / reviewCount) * 100 : 0;
              return (
                <div key={star} className="flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-600 w-8">{star}★</span>
                  <div className="flex-1 bg-gray-200 rounded-full h-2 overflow-hidden">
                    <div
                      className="bg-yellow-400 h-full transition-all duration-300"
                      style={{ width: `${percentage}%` }}
                    />
                  </div>
                  <span className="text-xs text-gray-500 w-12">{count}</span>
                </div>
              );
            })}
          </div>
        </div>

        {/* Write Review Button */}
        {!showReviewForm && (
          <button
            onClick={() => setShowReviewForm(true)}
            className="bg-blue-600 hover:bg-blue-700 text-white font-medium py-2 px-6 rounded-lg transition-colors flex items-center gap-2"
          >
            <MessageSquare size={18} />
            Write a Review
          </button>
        )}
      </div>

      {/* Review Form */}
      {showReviewForm && (
        <div className="bg-white border border-gray-200 rounded-lg p-6 mb-8">
          <h3 className="text-lg font-semibold mb-4">Share Your Experience</h3>

          <div className="mb-4">
            <label className="block text-sm font-medium text-gray-700 mb-2">Rating</label>
            <RatingStars
              rating={userRating}
              interactive
              onRate={(r) => setUserRating(r)}
            />
          </div>

          <div className="mb-4">
            <label className="block text-sm font-medium text-gray-700 mb-2">Review (optional)</label>
            <textarea
              value={userReviewText}
              onChange={(e) => setUserReviewText(e.target.value.slice(0, 500))}
              placeholder="Tell others about your experience with this server..."
              className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent resize-none"
              rows={4}
            />
            <div className="text-xs text-gray-500 mt-1">{userReviewText.length}/500</div>
          </div>

          <div className="flex gap-3">
            <button
              onClick={handleSubmitReview}
              disabled={submitting || userRating === 0}
              className="bg-blue-600 hover:bg-blue-700 disabled:bg-gray-400 text-white font-medium py-2 px-6 rounded-lg transition-colors"
            >
              {submitting ? 'Submitting...' : 'Submit Review'}
            </button>
            <button
              onClick={() => {
                setShowReviewForm(false);
                setUserRating(0);
                setUserReviewText('');
              }}
              className="bg-gray-200 hover:bg-gray-300 text-gray-800 font-medium py-2 px-6 rounded-lg transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Sort Options */}
      <div className="flex gap-2 mb-6">
        {(['recent', 'helpful', 'rating'] as const).map((s) => (
          <button
            key={s}
            onClick={() => setSort(s)}
            className={`px-4 py-2 rounded-lg font-medium transition-colors ${
              sort === s
                ? 'bg-blue-600 text-white'
                : 'bg-gray-200 text-gray-800 hover:bg-gray-300'
            }`}
          >
            {s.charAt(0).toUpperCase() + s.slice(1)}
          </button>
        ))}
      </div>

      {/* Reviews List */}
      {loading ? (
        <div className="space-y-4">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-24 bg-gray-100 rounded-lg animate-pulse" />
          ))}
        </div>
      ) : reviews.length === 0 ? (
        <div className="text-center py-8 text-gray-500">
          <MessageSquare size={32} className="mx-auto mb-2 opacity-50" />
          <p>No reviews yet. Be the first to review!</p>
        </div>
      ) : (
        <div className="space-y-4">
          {reviews.map((review) => (
            <div key={review.review_id} className="bg-white border border-gray-200 rounded-lg p-6">
              <div className="flex justify-between items-start mb-3">
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 bg-gradient-to-br from-blue-400 to-purple-400 rounded-full" />
                  <div>
                    <div className="font-medium text-gray-900">
                      User #{review.reviewer_user_id.toString().slice(-4)}
                      {review.is_verified_member && (
                        <span className="ml-2 text-xs bg-green-100 text-green-800 px-2 py-1 rounded">
                          Verified Member
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-gray-500">{formatDate(review.review_date)}</div>
                  </div>
                </div>
                <RatingStars rating={review.rating} />
              </div>

              {review.review_text && (
                <p className="text-gray-700 mb-3 leading-relaxed">{review.review_text}</p>
              )}

              <button
                onClick={() => handleMarkHelpful(review.review_id)}
                disabled={helpfulPending.has(review.review_id)}
                className="flex items-center gap-2 text-sm text-gray-600 hover:text-blue-600 transition-colors disabled:opacity-50"
              >
                <ThumbsUp size={16} />
                Helpful ({review.helpful_count})
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default ReviewsSection;
