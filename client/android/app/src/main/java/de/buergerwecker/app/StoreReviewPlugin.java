package de.buergerwecker.app;

import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.google.android.play.core.review.ReviewManager;
import com.google.android.play.core.review.ReviewManagerFactory;

// Google Play In-App Review (client/www/native.js, requestReview), called when
// the reader answers the check-in with "No, I've got one". Play rate-limits the
// sheet, shows it only in a copy installed from Google Play, and says nothing
// about whether it showed, so the call always resolves.
@CapacitorPlugin(name = "StoreReview")
public class StoreReviewPlugin extends Plugin {
    @PluginMethod
    public void requestReview(PluginCall call) {
        ReviewManager manager = ReviewManagerFactory.create(getContext());
        manager.requestReviewFlow().addOnCompleteListener(task -> {
            if (task.isSuccessful() && getActivity() != null) {
                manager.launchReviewFlow(getActivity(), task.getResult()).addOnCompleteListener(done -> call.resolve());
            } else {
                call.resolve();
            }
        });
    }
}
